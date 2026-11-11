import { createElement, forwardRef, useEffect, useImperativeHandle, useRef, type ReactNode } from 'react';

/** Tags Core's canonical Spec text may contain. Everything else is reduced to its text by the walkers below. */
const TAGS: Record<string, string> = { P: 'p', UL: 'ul', OL: 'ol', LI: 'li', STRONG: 'strong', B: 'strong', EM: 'em', I: 'em', BR: 'br' };
const DROP = new Set(['SCRIPT', 'STYLE', 'TEMPLATE', 'IFRAME', 'OBJECT']);

function parse(html: string): HTMLElement {
  return new DOMParser().parseFromString(`<body>${html}</body>`, 'text/html').body;
}

function toReact(node: Node, key: string): ReactNode {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent;
  if (node.nodeType !== Node.ELEMENT_NODE) return null;
  const element = node as HTMLElement;
  if (DROP.has(element.tagName)) return null;
  const children = [...element.childNodes].map((child, index) => toReact(child, `${key}.${index}`));
  const tag = TAGS[element.tagName];
  if (!tag) return children;
  return tag === 'br' ? createElement('br', { key }) : createElement(tag, { key }, ...children);
}

/** Safe, read-only rendering: only whitelisted elements are created and no attribute is ever copied. */
export function RichView({ html, className }: { html: string; className?: string }) {
  const body = parse(html);
  return <div className={className ?? 'rosc-rich'}>{[...body.childNodes].map((child, index) => toReact(child, String(index)))}</div>;
}

function fill(target: Element, source: Node): void {
  for (const child of [...source.childNodes]) {
    if (child.nodeType === Node.TEXT_NODE) { target.appendChild(document.createTextNode(child.textContent ?? '')); continue; }
    if (child.nodeType !== Node.ELEMENT_NODE) continue;
    const element = child as HTMLElement;
    if (DROP.has(element.tagName)) continue;
    const tag = TAGS[element.tagName];
    if (!tag) { fill(target, element); continue; }
    const made = document.createElement(tag);
    if (tag !== 'br') fill(made, element);
    target.appendChild(made);
  }
}

export type RichEditorHandle = { html: () => string; command: (name: string) => void; focus: () => void };
type EditorProps = { html: string; resetKey: string; label: string; onInput: () => void; className?: string };

/**
 * Minimal contenteditable editor (bold, italic, lists, paragraph). Its initial content is rebuilt from a whitelist, and
 * Core canonicalises again on save, so pasted markup can neither persist nor execute.
 */
export const RichEditor = forwardRef<RichEditorHandle, EditorProps>(function RichEditor({ html, resetKey, label, onInput, className }, ref) {
  const element = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const target = element.current;
    if (!target) return;
    target.replaceChildren();
    fill(target, parse(html));
  }, [html, resetKey]);
  useImperativeHandle(ref, () => ({
    html: () => element.current?.innerHTML ?? '',
    focus: () => element.current?.focus(),
    command: name => {
      const target = element.current;
      if (!target) return;
      target.focus();
      const exec = (document as Document & { execCommand?: (command: string, ui?: boolean, value?: string) => boolean }).execCommand;
      if (typeof exec === 'function') {
        try { if (name === 'paragraph') exec.call(document, 'formatBlock', false, 'p'); else exec.call(document, name, false); } catch { /* unsupported in this browser */ }
      }
      onInput();
    },
  }), [onInput]);
  return <div ref={element} className={className ?? 'rosc-rich rosc-rich--edit'} contentEditable suppressContentEditableWarning
    role="textbox" aria-multiline="true" aria-label={label} onInput={onInput}
    onPaste={event => {
      event.preventDefault();
      const text = event.clipboardData.getData('text/plain');
      const exec = (document as Document & { execCommand?: (command: string, ui?: boolean, value?: string) => boolean }).execCommand;
      if (typeof exec === 'function') exec.call(document, 'insertText', false, text);
    }} />;
});

type ToolbarProps = { editor: React.RefObject<RichEditorHandle | null>; attachLater?: boolean };

/** Toolbar of the approved Spec editor; the paper-clip stays visible but disabled until an Asset Store exists. */
export function RichToolbar({ editor, attachLater }: ToolbarProps) {
  const button = (command: string, label: string, content: ReactNode) =>
    <button type="button" aria-label={label} title={label} onMouseDown={event => event.preventDefault()}
      onClick={() => editor.current?.command(command)}>{content}</button>;
  return <div className="rosc-rich-toolbar" role="toolbar" aria-label="Форматирование">
    {button('bold', 'Жирный', <strong>B</strong>)}
    {button('italic', 'Курсив', <em>I</em>)}
    {button('insertUnorderedList', 'Маркированный список', '•')}
    {button('insertOrderedList', 'Нумерованный список', '1.')}
    {button('paragraph', 'Обычный текст', 'Т')}
    {attachLater && <button type="button" disabled aria-label="Прикрепить файл — будет доступно позже" title="Вложения появятся вместе с хранилищем файлов">📎</button>}
  </div>;
}
