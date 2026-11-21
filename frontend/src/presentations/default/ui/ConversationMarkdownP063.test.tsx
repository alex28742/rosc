import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { conversationMessage } from '../../../app/testing';
import { findMatches, MAX_MATCHES, renderBody, type Match } from './chatSearch';
import { ConversationMarkdown, MAX_MARKDOWN_CHARS, projectUncached, safeHref, visibleText } from './ConversationMarkdown';

// P063: one safe Markdown/GFM renderer for PC/TC bodies (published and provisional) plus visible-text search.
// Pure jsdom; no backend, no provider, no network.

afterEach(cleanup);

const md = (body: string, matches: Match[] = [], currentId: string | null = null) =>
  render(<>{renderBody(body, matches, currentId)}</>).container.querySelector('.rosc-chat-body') as HTMLElement;
const search = (bodies: string[], term: string) =>
  findMatches(bodies.map((body, index) => conversationMessage(body, { message_id: `m${index}`, ordinal: index + 1 })), term, 'x');
const show = (body: string, term: string) => {
  const matches = search([body], term);
  return { matches, root: md(body, matches, matches[0]?.id ?? null) };
};

/** No element may hold a block it cannot legally contain (the classic `<p><table>` / `<p><pre>` mistake). */
function assertValidNesting(root: HTMLElement) {
  const blocks = 'p, div, pre, table, ul, ol, blockquote, h1, h2, h3, h4, h5, h6, hr';
  for (const p of root.querySelectorAll('p, h1, h2, h3, h4, h5, h6, span, strong, em, code, a, del, mark')) {
    expect(p.querySelector(blocks), `${p.tagName} contains a block`).toBeNull();
  }
  for (const el of root.querySelectorAll('*')) {
    for (const attr of el.getAttributeNames()) expect(attr.startsWith('on'), `${el.tagName}[${attr}]`).toBe(false);
    expect(el.hasAttribute('style')).toBe(false);
  }
}

describe('P063 CommonMark + GFM rendering', () => {
  it('renders blocks and inlines with valid structure and no literal delimiters', () => {
    const root = md([
      '# Заголовок', '', 'Абзац с **жирным**, *курсивом*, ~~зачёркнутым~~ и `кодом`.', 'Вторая строка.', '',
      '- один', '- два', '  1. вложенный', '', '> цитата', '', '---', '',
      '```ts', 'const x = 1;   // spaces kept', '```', '',
      '| A | B |', '|:-|-:|', '| 1 | **2** |', '', '- [x] done', '- [ ] todo', '', '[ссылка](https://example.com/a)',
    ].join('\n'));
    expect(root.querySelector('h1')?.textContent).toBe('Заголовок');
    expect(root.querySelector('strong')?.textContent).toBe('жирным');
    expect(root.querySelector('em')?.textContent).toBe('курсивом');
    expect(root.querySelector('del')?.textContent).toBe('зачёркнутым');
    expect(root.querySelector('code.rosc-md-code')?.textContent).toBe('кодом');
    expect(root.querySelector('p')?.textContent).toContain('\nВторая строка.'); // soft break kept as a line break
    expect(root.querySelector('ul > li > ol > li')?.textContent).toBe('вложенный');
    expect(root.querySelector('blockquote')?.textContent).toBe('цитата');
    expect(root.querySelector('hr')).toBeTruthy();
    const pre = root.querySelector('pre.rosc-md-pre') as HTMLElement;
    expect(pre.querySelector('code')?.className).toBe('language-ts');
    expect(pre.textContent).toBe('const x = 1;   // spaces kept');
    const table = root.querySelector('.rosc-md-table-wrap > table') as HTMLTableElement;
    expect([...table.querySelectorAll('th')].map(th => th.textContent)).toEqual(['A', 'B']);
    expect(table.querySelector('td.rosc-md-align-right strong')?.textContent).toBe('2');
    const boxes = [...root.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')];
    expect(boxes.map(box => [box.checked, box.disabled])).toEqual([[true, true], [false, true]]);
    expect(root.textContent).not.toMatch(/\*\*|~~|```|\|/);
    assertValidNesting(root);
  });

  it('keeps a plain, an empty and an @mention message readable as before', () => {
    expect(md('Просто текст').querySelector('p')?.textContent).toBe('Просто текст');
    expect(md('').textContent).toBe('');
    expect(md('@Architect посмотри, пожалуйста').textContent).toBe('@Architect посмотри, пожалуйста');
    const emoji = md('Привет 👋🏽 мир\nстрока 2 — 日本語');
    expect(emoji.textContent).toBe('Привет 👋🏽 мир\nстрока 2 — 日本語');
  });

  it('language of a fence is only a sanitised class; an exotic info string is dropped', () => {
    const root = md('```" onmouseover="x\ncode\n```');
    expect(root.querySelector('pre code')?.className).toBe('');
    assertValidNesting(root);
  });
});

describe('P063 safety: untrusted text never becomes active content', () => {
  it('raw HTML (inline and block) is shown literally, never interpreted', () => {
    const body = '<script>window.p063=1</script>\n\n<div onclick="x()">блок</div>\n\nи <img src=x onerror="alert(1)"> <b>b</b> <iframe src="https://evil"></iframe>';
    const root = md(body);
    expect(root.querySelector('script, img, b, iframe, svg')).toBeNull();
    expect(root.textContent).toContain('<script>window.p063=1</script>');
    expect(root.textContent).toContain('<img src=x onerror="alert(1)">');
    expect((window as unknown as { p063?: number }).p063).toBeUndefined();
    assertValidNesting(root);
  });

  it('allows only http(s), mailto and in-page # links; external ones are isolated', () => {
    expect(safeHref('https://example.com/x?a=1')).toBe('https://example.com/x?a=1');
    expect(safeHref('mailto:a@b.c')).toBe('mailto:a@b.c');
    expect(safeHref('#section')).toBe('#section');
    for (const bad of ['javascript:alert(1)', 'JaVaScRiPt:alert(1)', ' javascript:x', 'java\tscript:x', 'data:text/html,x',
      'vbscript:x', 'file:///etc/passwd', '/relative', 'relative', '//evil.example', '', 'http:/broken']) {
      expect(safeHref(bad), bad).toBeNull();
    }
    const root = md([
      '[ok](https://example.com)', '[js](javascript:alert(1))', '[enc](&#106;avascript:alert(1))',
      '[pct](java%73cript:alert(1))', '[data](data:text/html;base64,PHNjcmlwdD4=)', '<javascript:alert(1)>',
      '[mail](mailto:o@example.com)', '[ref][r]', '', '[r]: javascript:alert(2)', 'www.example.org',
    ].join('\n'));
    const anchors = [...root.querySelectorAll('a')];
    expect(anchors.map(a => a.getAttribute('href'))).toEqual(['https://example.com', 'mailto:o@example.com', 'http://www.example.org']);
    expect(anchors[0].getAttribute('target')).toBe('_blank');
    expect(anchors[0].getAttribute('rel')).toBe('noopener noreferrer');
    expect(anchors[1].hasAttribute('target')).toBe(false);
    // js, encoded js, %-encoded js, data:, the <javascript:> autolink and the javascript: reference: text kept, no href
    expect(root.querySelectorAll('.rosc-md-link--blocked').length).toBe(6);
    expect(root.textContent).toContain('js');
    assertValidNesting(root);
  });

  it('image syntax never creates <img> or a network request: a text placeholder only', () => {
    const root = md('![трекер](https://evil.example/pixel.gif) ![](data:image/png;base64,AAA)\n\n![r][i]\n\n[i]: https://x/y.png');
    expect(root.querySelector('img')).toBeNull();
    expect([...root.querySelectorAll('.rosc-md-image')].map(el => el.textContent))
      .toEqual(['[изображение: трекер]', '[изображение]', '[изображение: r]']);
    expect(root.innerHTML).not.toContain('evil.example');
  });

  it('a huge or pathologically nested body falls back to escaped plain text', () => {
    const big = `${'**x** '.repeat(MAX_MARKDOWN_CHARS / 6 + 10)}<script>bad()</script>`;
    expect(projectUncached(big).root).toBeNull();
    const root = md(big);
    expect(root.querySelector('strong, script')).toBeNull();
    expect(root.textContent).toBe(big);
    const deep = `${'>'.repeat(2000)} глубоко`;
    expect(projectUncached(deep).root).toBeNull();
    expect(md(deep).textContent).toBe(deep);
  });
});

describe('P063 streaming: partial Markdown never breaks, loses or duplicates text', () => {
  const chunks = (text: string, size: number) => Array.from({ length: Math.ceil(text.length / size) }, (_, i) => text.slice(i * size, (i + 1) * size));
  const structural = [
    'Начинаю **важный** разбор *гипотез* и `inline` кода 😀.', '', '```python', 'def f(x):', '    return x  # отступ', '```', '',
    '| Колонка | Значение |', '| --- | --- |', '| a | [ссылка](https://example.com/p) |', '', '- пункт ~~старый~~', '',
  ].join('\n');
  const full = `${structural}\n\n${'Длинная строка '.repeat(80)}`;

  it('every prefix renders without throwing, without HTML injection and with valid nesting', () => {
    const caret = <span className="rosc-chat-caret" aria-hidden="true" />;
    const { rerender, container } = render(<ConversationMarkdown body="" transient tail={caret} />);
    for (const [source, size] of [[structural, 1], [full, 7], [full, 29]] as const) {
      let text = '';
      for (const piece of chunks(source, size)) {
        text += piece;
        rerender(<ConversationMarkdown body={text} transient tail={caret} />);
        const root = container.querySelector('.rosc-chat-body') as HTMLElement;
        expect(container.querySelectorAll('.rosc-chat-caret')).toHaveLength(1);
        expect(root.querySelector('pre .rosc-chat-caret, table .rosc-chat-caret')).toBeNull();
        expect(root.querySelector('script, img, iframe')).toBeNull();
        assertValidNesting(root);
      }
    }
    // The final state is the full formatted answer, received text intact.
    const root = container.querySelector('.rosc-chat-body') as HTMLElement;
    expect(root.querySelector('strong')?.textContent).toBe('важный');
    expect(root.querySelector('pre')?.textContent).toBe('def f(x):\n    return x  # отступ');
    expect(root.querySelector('td a')?.getAttribute('href')).toBe('https://example.com/p');
    expect(root.textContent).toContain('Длинная строка Длинная строка');
  });

  it('unclosed delimiters stay as visible text until closed', () => {
    expect(md('```j').querySelector('pre')).toBeTruthy(); // an open fence is already a (still empty) code block
    for (const partial of ['**жир', '*кур', '`код', '```js\nconst a', '| a | b', '| a | b |\n| -', '[lab', '[label](', '[label](https://ex']) {
      const root = md(partial);
      expect(root.textContent?.length, partial).toBeGreaterThan(0);
      assertValidNesting(root);
    }
    expect(md('```js\nconst a').querySelector('pre')?.textContent).toBe('const a'); // an open fence already shows code
    expect(md('**жир').textContent).toBe('**жир');
  });
});

describe('P063 search over the visible text', () => {
  it('finds text inside formatting and highlights within the formatted node', () => {
    const { matches, root } = show('Это **важно** знать', 'важно');
    expect(matches).toHaveLength(1);
    const mark = root.querySelector('strong > mark') as HTMLElement;
    expect(mark.textContent).toBe('важно');
    expect(mark.id).toBe(matches[0].id);
    expect(mark.classList.contains('rosc-chat-mark--current')).toBe(true);
  });

  it('never searches hidden delimiters or link destinations', () => {
    expect(search(['**bold** and __x__'], '**')).toHaveLength(0);
    expect(search(['[label](https://example.com/secret-path)'], 'secret')).toHaveLength(0);
    expect(search(['[label](https://example.com/secret-path)'], 'label')).toHaveLength(1);
    expect(search(['```ts\nx\n```'], '```')).toHaveLength(0);
    expect(search(['![alt](https://evil/hidden.png)'], 'hidden')).toHaveLength(0);
    expect(search(['<b>raw</b>'], '<b>')).toHaveLength(1); // literal HTML IS visible text
  });

  it('code, tables and lists take part in search', () => {
    const body = '- элемент альфа\n\n| h |\n|---|\n| ячейка альфа |\n\n```\nкод альфа\n```\n\n`инлайн альфа`';
    const { matches, root } = show(body, 'альфа');
    expect(matches).toHaveLength(4);
    expect(root.querySelector('li mark')).toBeTruthy();
    expect(root.querySelector('td mark')).toBeTruthy();
    expect(root.querySelector('pre mark')).toBeTruthy();
    expect(root.querySelector('code.rosc-md-code mark')).toBeTruthy();
  });

  it('a match across formatted segments is highlighted piecewise, one id, documented', () => {
    const { matches, root } = show('ва**жно** дело', 'важно');
    expect(matches).toHaveLength(1);
    const pieces = [...root.querySelectorAll<HTMLElement>('mark.rosc-chat-mark')];
    expect(pieces.map(piece => piece.textContent)).toEqual(['ва', 'жно']);
    expect(pieces.every(piece => piece.dataset.roscMatch === matches[0].id)).toBe(true);
    expect(pieces.filter(piece => piece.id)).toHaveLength(1);
    expect(pieces[0].id).toBe(matches[0].id);
    expect(pieces[1].closest('strong')).toBeTruthy();
  });

  it('a term never joins two blocks', () => {
    expect(visibleText('конец\n\nначало')).toBe('конец\nначало');
    expect(search(['конец\n\nначало'], 'конецначало')).toHaveLength(0);
  });

  it('keeps the 500 cap and unique ids; plain fallback search still highlights', () => {
    const many = search(['a '.repeat(MAX_MATCHES + 50)], 'a');
    expect(many).toHaveLength(MAX_MATCHES);
    expect(new Set(many.map(match => match.id)).size).toBe(MAX_MATCHES);
    const big = `${'z'.repeat(MAX_MARKDOWN_CHARS)} **цель**`;
    const { matches, root } = show(big, 'цель');
    expect(matches).toHaveLength(1);
    expect(root.querySelector('mark')?.textContent).toBe('цель');
    expect(root.querySelector('strong')).toBeNull();
  });
});
