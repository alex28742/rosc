import { useEffect, useRef, useState, type FormEvent } from 'react';
import type { MessageRecipient } from '../../../core/models';
import type { TaskConversationControls } from '../../../app/presentation/contracts';
import { architectReason, describeArchitect } from './architectView';

/** Delivery wording is about the stored Owner message, never proof that the Architect answered. */
const DELIVERY_TEXT: Record<string, string> = {
  pending: 'Отправлено Architect, ожидается ответ…',
  delivered: 'Architect ответил в этой задаче',
  failed: 'Не доставлено Architect',
  unknown: 'Результат доставки неизвестен — повторная отправка не выполнялась',
};

function formatTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

/** One client-generated opaque idempotency key per unsent message; retrying the same text and addressee reuses it. */
function newClientKey(): string {
  const random = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `ui-${random}`.replace(/[^A-Za-z0-9._:-]/g, '').slice(0, 96);
}

/**
 * Task Conversation: this Task's own saved history and an Owner composer. Saving and sending are separate: a message
 * addressed to the Architect is handed over only when Core reports a verified live Architect (or by the explicit send
 * action); the reply, delivery state and failures are shown exactly as Core reports them.
 */
type Props = { taskId: string; conversation: TaskConversationControls; collapsed: boolean; onCollapsedChange: (collapsed: boolean) => void };

export function TaskConversationPanel({ taskId, conversation, collapsed, onCollapsedChange }: Props) {
  const state = conversation.state;
  const [draft, setDraft] = useState('');
  const [recipient, setRecipient] = useState<MessageRecipient>(null);
  const [failure, setFailure] = useState('');
  const [locked, setLocked] = useState(false);
  const [deliveryFailure, setDeliveryFailure] = useState('');
  const view = describeArchitect(conversation.architect.info, conversation.architect.status === 'ready', false);
  const live = view.live && conversation.architect.info?.state === 'ready';
  const pending = useRef<{ body: string; recipient: MessageRecipient; key: string } | null>(null);
  const inFlight = useRef(false);
  const feed = useRef<HTMLDivElement | null>(null);
  const preserveOlder = useRef<number | null>(null);
  const ready = state.status === 'ready';
  const lastId = state.messages.length > 0 ? state.messages[state.messages.length - 1].message_id : null;

  useEffect(() => {
    if (feed.current) feed.current.scrollTop = feed.current.scrollHeight;
  }, [lastId, state.status]);

  useEffect(() => {
    const element = feed.current;
    if (!element || preserveOlder.current === null) return;
    element.scrollTop = element.scrollHeight - preserveOlder.current;
    preserveOlder.current = null;
  }, [state.messages.length]);

  const loadOlder = () => {
    if (!feed.current || !state.hasMore || state.olderLoading) return;
    preserveOlder.current = feed.current.scrollHeight - feed.current.scrollTop;
    conversation.onLoadOlder();
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const body = draft.trim();
    if (!body || !ready || state.sending || inFlight.current) return;
    inFlight.current = true;
    setLocked(true);
    const same = pending.current?.body === body && pending.current.recipient === recipient;
    const key = same && pending.current ? pending.current.key : newClientKey();
    pending.current = { body, recipient, key };
    setFailure('');
    try {
      const problem = await conversation.onSend(body, key, recipient);
      if (problem === null) {
        setDraft(current => current.trim() === body ? '' : current);
        pending.current = null;
      } else setFailure(problem);
    } finally {
      inFlight.current = false;
      setLocked(false);
    }
  };

  return <aside className={collapsed ? 'rosc-conversation rosc-conversation--task rosc-conversation--collapsed' : 'rosc-conversation rosc-conversation--task'}
    aria-label="Task Conversation" data-task-id={taskId}>
    <button type="button" className="rosc-conversation__collapse" aria-label="Свернуть Task Conversation"
      title="Свернуть Task Conversation вправо" onClick={() => onCollapsedChange(true)}>›</button>
    <header className="rosc-conversation__head">
      <div>
        <h3>Task Conversation</h3>
        <p>{live ? 'История задачи в ROSC · сообщения для Architect передаются ему, ответ придёт сюда'
          : 'История задачи сохраняется в ROSC · Architect не активен — сообщения только сохраняются'}</p>
        <p className={`rosc-conversation__architect rosc-conversation__architect--${view.tone}`}>{view.label}</p>
      </div>
      <div className="rosc-conversation__head-actions">
        <button type="button" className="rosc-chat-icon" disabled aria-label="Поиск по Task Conversation — пока недоступно"
          title="Поиск будет доступен позже">⌕</button>
      </div>
    </header>
    <div className="rosc-conversation__feed" role="log" aria-label="Сообщения по задаче" ref={feed}>
      {state.hasMore && ready && <div className="rosc-conversation__more">
        <button type="button" className="rosc-btn rosc-btn--small" onClick={loadOlder} disabled={state.olderLoading}>
          {state.olderLoading ? 'Загрузка…' : 'Загрузить более ранние'}
        </button>
      </div>}
      {state.status === 'loading' && <div className="rosc-state rosc-state--loading"><span className="rosc-spinner"
        aria-hidden="true" />Загрузка переписки…</div>}
      {state.status === 'failed' && <div className="rosc-state rosc-state--failed" role="alert">
        <p>Не удалось загрузить переписку. {state.error}</p>
        <button className="rosc-btn rosc-btn--small" onClick={conversation.onReload}>Повторить</button>
      </div>}
      {ready && state.messages.length === 0 && <div className="rosc-state">
        <p>Сообщений по этой задаче пока нет.</p>
        <p>{live ? 'Сообщение с адресатом Architect будет ему передано, ответ сохранится здесь.'
          : 'Сообщение Owner сохранится в ROSC. Architect не активен, поэтому ответа не будет.'}</p>
      </div>}
      {state.messages.map(message => {
        const fromArchitect = message.sender.kind === 'architect';
        const delivery = message.delivery;
        const addressed = message.recipient === 'architect';
        return <article key={message.message_id}
          className={fromArchitect ? 'rosc-chat-message rosc-chat-message--architect' : 'rosc-chat-message rosc-chat-message--owner'}
          data-message-id={message.message_id}>
          <span className="rosc-chat-avatar" aria-hidden="true">{fromArchitect ? 'A' : 'O'}</span>
          <div className="rosc-chat-bubble">
            <div className="rosc-chat-meta">
              <span>{message.sender.display_name}{addressed && !fromArchitect ? ' → Architect' : ''}</span>
              <time dateTime={message.created_at}>{formatTime(message.created_at)}</time>
            </div>
            <p className="rosc-chat-body">{message.body}</p>
            {!fromArchitect && (!delivery || delivery.state === 'not_sent') && <p className="rosc-chat-delivery">
              {addressed ? 'Сохранено · не отправлено Architect' : 'Сохранено'}
              {addressed && <button type="button" className="rosc-btn rosc-btn--small" disabled={!live}
                title={live ? undefined : 'Architect не активен — активируйте его в Project Conversation'}
                onClick={async () => setDeliveryFailure(await conversation.onSendToArchitect(message.message_id) ?? '')}>
                Отправить Architect</button>}
            </p>}
            {delivery && delivery.state !== 'not_sent' && <p className={`rosc-chat-delivery rosc-chat-delivery--${delivery.state}`}
              aria-live="polite">
              {DELIVERY_TEXT[delivery.state]}{delivery.error_code ? ` (${architectReason(delivery.error_code) || delivery.error_code})` : ''}
              {delivery.can_retry && <button type="button" className="rosc-btn rosc-btn--small"
                onClick={async () => setDeliveryFailure(await conversation.onRetryDelivery(message.message_id) ?? '')}>
                Повторить отправку</button>}
              {delivery.can_reconcile && (delivery.state === 'unknown' || delivery.state === 'failed') && <button type="button"
                className="rosc-btn rosc-btn--small"
                onClick={async () => setDeliveryFailure(await conversation.onReconcile(message.message_id) ?? '')}>
                {delivery.state === 'failed' ? 'Восстановить ответ' : 'Проверить результат'}</button>}
            </p>}
          </div>
        </article>;
      })}
    </div>
    <form className="rosc-conversation__compose" onSubmit={submit}>
      <textarea aria-label="Сообщение в Task Conversation" placeholder="Заметка или вопрос по этой задаче…" rows={3}
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
          <select className="rosc-chat-tool" aria-label="Адресат сообщения" value={recipient ?? ''} disabled={locked}
            onChange={event => setRecipient(event.target.value === 'architect' ? 'architect' : null)}>
            <option value="">@ Адресаты ▾</option>
            <option value="architect">@ Architect</option>
            <option value="worker" disabled>@ Worker — будет доступно позже</option>
          </select>
          <small>От: Owner · local_unverified</small>
        </div>
        <button type="submit" className="rosc-btn rosc-btn--primary"
          disabled={!draft.trim() || !ready || state.sending || locked}>{state.sending ? 'Сохранение…'
            : recipient === 'architect' && live ? 'Отправить' : 'Сохранить'}</button>
      </div>
      {failure && <p className="rosc-conversation__error" role="alert">{failure}</p>}
      {deliveryFailure && <p className="rosc-conversation__error" role="alert">{deliveryFailure}</p>}
      {!ready && <p className="rosc-conversation__hint">Сохранение станет доступно после загрузки истории.</p>}
      <p className="rosc-conversation__hint">
        {recipient === 'architect'
          ? (live ? 'Сообщение сохранится в ROSC и будет передано Architect; ранее сохранённые сообщения не пересылаются.'
            : 'Architect не активен: сообщение сохранится в ROSC с адресатом Architect, его можно отправить позже кнопкой «Отправить Architect».')
          : 'Сообщение сохранится в ROSC без адресата и никому не передаётся.'}
      </p>
    </form>
    <button type="button" className="rosc-conversation__rail" aria-label="Развернуть Task Conversation"
      onClick={() => onCollapsedChange(false)}>
      <span aria-hidden="true">‹</span>
      <span className="rosc-conversation__rail-label">TASK CONVERSATION</span>
    </button>
  </aside>;
}
