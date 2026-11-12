import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import type { ConversationControls } from '../../../app/presentation/contracts';
import { architectReason, describeArchitect } from './architectView';
import { findMatches, renderBody } from './chatSearch';

type Props = {
  projectId: string;
  conversation: ConversationControls;
  collapsed: boolean;
  onCollapsedChange: (collapsed: boolean) => void;
};

/** Delivery wording is about the stored Owner message, never proof that the Architect answered. */
const DELIVERY_TEXT: Record<string, string> = {
  pending: 'Передано Architect, ожидается ответ…',
  delivered: 'Architect ответил',
  failed: 'Не доставлено Architect',
  unknown: 'Результат доставки неизвестен — повторная отправка не выполнялась',
};

function formatTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}

/** One client-generated opaque idempotency key per unsent message; retrying the same text reuses it. */
function newClientKey(): string {
  const random = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `ui-${random}`.replace(/[^A-Za-z0-9._:-]/g, '').slice(0, 96);
}

/**
 * Persistent right-hand Project Conversation: ordered Owner messages, a truthful empty state, a bounded composer,
 * collapse/restore and client-local search. It shows delivery and an Architect reply only when Core reports them,
 * and an Architect as active only after Core's verified `ready` state.
 */
export function ConversationPanel({ projectId, conversation, collapsed, onCollapsedChange }: Props) {
  const state = conversation.state;
  const architect = conversation.architect;
  const view = describeArchitect(architect.info, architect.status === 'ready', architect.busy);
  const live = view.live && architect.info?.state === 'ready';
  const unresolved = architect.info?.state === 'error' && architect.info.can_deactivate;
  const [deliveryFailure, setDeliveryFailure] = useState('');
  const [draft, setDraft] = useState('');
  const [failure, setFailure] = useState('');
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchTerm, setSearchTerm] = useState('');
  const [matchIndex, setMatchIndex] = useState(0);
  /** The last submitted body with its key. Kept after a failure so an unchanged retry replays the same key. */
  const pending = useRef<{ body: string; key: string } | null>(null);
  const inFlight = useRef(false);
  const [locked, setLocked] = useState(false);
  const feed = useRef<HTMLDivElement | null>(null);
  const preserveOlder = useRef<number | null>(null);

  const matches = useMemo(() => findMatches(state.messages, searchTerm.trim()), [state.messages, searchTerm]);
  const currentMatch = matches.length === 0 ? -1 : Math.min(matchIndex, matches.length - 1);
  const currentId = currentMatch >= 0 ? matches[currentMatch].id : null;
  const lastMessageId = state.messages.length > 0 ? state.messages[state.messages.length - 1].message_id : null;

  // New messages scroll to the bottom; prepending older ones keeps the reader where they were.
  useEffect(() => {
    if (searchTerm.trim() || !feed.current) return;
    feed.current.scrollTop = feed.current.scrollHeight;
  }, [lastMessageId, state.status, searchTerm]);

  useEffect(() => {
    const element = feed.current;
    if (!element || preserveOlder.current === null) return;
    element.scrollTop = element.scrollHeight - preserveOlder.current;
    preserveOlder.current = null;
  }, [state.messages.length]);

  useEffect(() => {
    if (!currentId) return;
    document.getElementById(currentId)?.scrollIntoView?.({ block: 'center' });
  }, [currentId]);

  const loadOlder = () => {
    if (!feed.current || !state.hasMore || state.olderLoading) return;
    preserveOlder.current = feed.current.scrollHeight - feed.current.scrollTop;
    conversation.onLoadOlder();
  };

  const toggleSearch = () => {
    if (searchOpen) {
      setSearchOpen(false);
      setSearchTerm('');
      setMatchIndex(0);
    } else setSearchOpen(true);
  };

  const step = (delta: number) => {
    if (matches.length === 0) return;
    setMatchIndex(current => (current + delta + matches.length) % matches.length);
  };

  const ready = state.status === 'ready';

  /**
   * The composer is read-only while a send is in flight, so text typed after submission cannot exist and a success
   * can only clear exactly what was submitted. A retry of an unchanged body reuses its key; a changed body gets a
   * fresh key because replaying the old one with different content would be rejected as a conflict.
   */
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const body = draft.trim();
    if (!body || !ready || state.sending || inFlight.current) return;
    inFlight.current = true;
    setLocked(true);
    const key = pending.current?.body === body ? pending.current.key : newClientKey();
    pending.current = { body, key };
    setFailure('');
    try {
      const problem = await conversation.onSend(body, key);
      if (problem === null) {
        setDraft(current => current.trim() === body ? '' : current);
        pending.current = null;
        setMatchIndex(0);
      } else setFailure(problem);
    } finally {
      inFlight.current = false;
      setLocked(false);
    }
  };
  const ambiguous = failure !== '' && pending.current !== null && draft.trim() !== pending.current.body;

  return <aside className={collapsed ? 'rosc-conversation rosc-conversation--collapsed' : 'rosc-conversation'}
    aria-label="Project Conversation" data-project-id={projectId}>
    <button type="button" className="rosc-conversation__collapse" aria-label="Свернуть Project Conversation"
      title="Свернуть Project Conversation вправо" onClick={() => onCollapsedChange(true)}>›</button>
    <header className="rosc-conversation__head">
      <div>
        <h3>Project Conversation</h3>
        <p>{live ? 'История сохраняется в ROSC · новые сообщения передаются Architect'
          : unresolved ? 'История сохраняется в ROSC · runtime требует безопасного разрешения'
            : 'История сохраняется в ROSC · Architect не активирован'}</p>
      </div>
      <div className="rosc-conversation__head-actions">
        <button type="button" className={searchOpen ? 'rosc-chat-icon rosc-chat-icon--on' : 'rosc-chat-icon'}
          aria-label="Поиск по сообщениям" aria-expanded={searchOpen} title="Поиск по Project Conversation"
          onClick={toggleSearch}>⌕</button>
      </div>
    </header>
    {searchOpen && <div className="rosc-conversation__search">
      <input type="search" aria-label="Поиск по сообщениям" placeholder="Найти в переписке…"
        value={searchTerm} autoFocus
        onChange={event => { setSearchTerm(event.target.value); setMatchIndex(0); }}
        onKeyDown={event => {
          if (event.key === 'Enter') { event.preventDefault(); step(event.shiftKey ? -1 : 1); }
          if (event.key === 'Escape') { event.stopPropagation(); toggleSearch(); }
        }} />
      <span className="rosc-conversation__count" aria-live="polite">
        {matches.length === 0 ? '0 / 0' : `${currentMatch + 1} / ${matches.length}`}
      </span>
      <button type="button" className="rosc-chat-nav" aria-label="Предыдущее совпадение"
        onClick={() => step(-1)} disabled={matches.length === 0}>‹</button>
      <button type="button" className="rosc-chat-nav" aria-label="Следующее совпадение"
        onClick={() => step(1)} disabled={matches.length === 0}>›</button>
    </div>}
    <div className="rosc-conversation__feed" role="log" aria-label="Сообщения по проекту" ref={feed}>
      {state.hasMore && state.status === 'ready' && <div className="rosc-conversation__more">
        <button type="button" className="rosc-btn rosc-btn--small" onClick={loadOlder} disabled={state.olderLoading}>
          {state.olderLoading ? 'Загрузка…' : 'Загрузить более ранние'}
        </button>
        <span>Поиск охватывает загруженные сообщения</span>
      </div>}
      {state.status === 'loading' && <div className="rosc-state rosc-state--loading"><span className="rosc-spinner"
        aria-hidden="true" />Загрузка переписки…</div>}
      {state.status === 'failed' && <div className="rosc-state rosc-state--failed" role="alert">
        <p>Не удалось загрузить переписку. {state.error}</p>
        <button className="rosc-btn rosc-btn--small" onClick={conversation.onReload}>Повторить</button>
      </div>}
      {state.status === 'ready' && state.messages.length === 0 && <div className="rosc-state">
        <p>Сообщений пока нет.</p>
        <p>{live ? 'Новое сообщение Owner будет передано Architect, ответ сохранится в этой переписке.'
          : unresolved ? 'Первое сообщение Owner сохранится в ROSC. Runtime недоступен, доставки и ответа не будет.'
            : 'Первое сообщение Owner сохранится в ROSC. Architect не активирован, поэтому ответа не будет.'}</p>
      </div>}
      {state.messages.map(message => {
        const fromArchitect = message.sender.kind === 'architect';
        const delivery = message.delivery;
        return <article key={message.message_id}
          className={fromArchitect ? 'rosc-chat-message rosc-chat-message--architect' : 'rosc-chat-message rosc-chat-message--owner'}
          data-message-id={message.message_id}>
          <span className="rosc-chat-avatar" aria-hidden="true">{fromArchitect ? 'A' : 'O'}</span>
          <div className="rosc-chat-bubble">
            <div className="rosc-chat-meta">
              <span>{message.sender.display_name}</span>
              <time dateTime={message.created_at}>{formatTime(message.created_at)}</time>
            </div>
            <p className="rosc-chat-body">{renderBody(message.body,
              matches.filter(match => match.messageId === message.message_id), currentId)}</p>
            {delivery && delivery.state !== 'not_sent' && <p className={`rosc-chat-delivery rosc-chat-delivery--${delivery.state}`}
              aria-live="polite">
              {DELIVERY_TEXT[delivery.state]}{delivery.error_code ? ` (${architectReason(delivery.error_code) || delivery.error_code})` : ''}
              {delivery.can_retry && <button type="button" className="rosc-btn rosc-btn--small"
                onClick={async () => setDeliveryFailure(await conversation.onRetryDelivery(message.message_id) ?? '')}>
                Повторить отправку</button>}
              {delivery.can_reconcile && (delivery.state === 'unknown' || delivery.state === 'failed') && <button type="button" className="rosc-btn rosc-btn--small"
                onClick={async () => setDeliveryFailure(await conversation.onReconcile(message.message_id) ?? '')}>
                {delivery.state === 'failed' ? 'Восстановить ответ' : 'Проверить результат'}</button>}
            </p>}
          </div>
        </article>;
      })}
    </div>
    <form className="rosc-conversation__compose" onSubmit={submit}>
      <textarea aria-label="Сообщение в Project Conversation" placeholder="Обсудить задачу, задать вопрос…" rows={3}
        value={draft} readOnly={locked} aria-busy={locked}
        onChange={event => setDraft(event.target.value)}
        onKeyDown={event => {
          if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
            event.preventDefault();
            event.currentTarget.form?.requestSubmit();
          }
        }} />
      <div className="rosc-conversation__actions">
        <div className="rosc-conversation__recipients">
          <button type="button" className="rosc-chat-tool" disabled title="Вложения пока недоступны"
            aria-label="Прикрепить файлы — пока недоступно">📎</button>
          <button type="button" className="rosc-chat-tool" disabled title="Адресация участников пока недоступна"
            aria-label="@ Адресаты — пока недоступно">@ Адресаты ▾</button>
          <small>От: Owner · local_unverified</small>
        </div>
        <button type="submit" className="rosc-btn rosc-btn--primary"
          disabled={!draft.trim() || !ready || state.sending || locked}>{state.sending ? 'Сохранение…' : 'Отправить'}</button>
      </div>
      {failure && <p className="rosc-conversation__error" role="alert">{failure}</p>}
      {ambiguous && <p className="rosc-conversation__hint">
        Предыдущая попытка могла сохраниться в ROSC. Проверьте историю: изменённый текст будет отправлен как новое сообщение.
      </p>}
      {!ready && <p className="rosc-conversation__hint">Отправка станет доступна после загрузки истории.</p>}
      {deliveryFailure && <p className="rosc-conversation__error" role="alert">{deliveryFailure}</p>}
      <p className="rosc-conversation__hint">
        {live ? 'Новое сообщение сохраняется в ROSC и передаётся Architect. Ранее сохранённые сообщения не пересылаются.'
          : unresolved ? 'Сообщение сохраняется только в ROSC. Runtime требует безопасного разрешения; доставки и ответа не будет.'
            : 'Сообщение сохраняется только в ROSC. Architect не активирован — доставки и ответа не будет.'}
      </p>
    </form>
    <button type="button" className="rosc-conversation__rail" aria-label="Развернуть Project Conversation"
      onClick={() => onCollapsedChange(false)}>
      <span aria-hidden="true">‹</span>
      <span className="rosc-conversation__rail-label">PROJECT CONVERSATION</span>
    </button>
  </aside>;
}
