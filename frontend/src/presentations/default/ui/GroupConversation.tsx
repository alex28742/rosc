import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import type { GroupControls } from '../../../app/presentation/contracts';
import type { ConversationMessage, GroupStatus, GroupTurn } from '../../../core/models';
import { findMentions } from './mentions';

/**
 * P046 group conversation pieces shared by the Project (PC) and Task (TC) conversation panels. Everything rendered
 * here is Core-reported state; nothing is simulated. The r05 prototype's demo-only controls (synthetic publish/failure/
 * UNKNOWN, preset stages, session simulation, reset) are intentionally absent.
 */

export const UNSUPPORTED_TEXT: Record<string, string> = {
  architect_group_not_integrated: 'Групповые ходы Architect недоступны в этой сборке — отправьте ему отдельным сообщением',
  architect_not_active: 'Architect не активен: активируйте его, чтобы позвать в групповой разговор',
  no_active_architect_assignment: 'Architect не назначен этому проекту',
  adapter_not_supported: 'У этого адаптера нет транспорта для чата (в v1.0 — только Claude Code)',
};

const TURN_ERROR_TEXT: Record<string, string> = {
  launch_failed: 'CLI не запустился', cli_unavailable: 'CLI не найден', unsupported_lane: 'адаптер не поддерживается',
  adapter_not_supported: 'адаптер не поддерживается', model_invalid: 'некорректная модель агента', lane_busy: 'сессия агента занята',
  timeout: 'истекло время ожидания', no_result: 'процесс завершился без результата', native_session_missing: 'нативная сессия не найдена',
  evidence_missing: 'evidence отсутствует', evidence_corrupt: 'evidence повреждено', evidence_mismatch: 'evidence не совпадает',
  submit_outcome_lost: 'результат отправки потерян (перезапуск)', transport_error: 'ошибка транспорта', root_unavailable: 'корень проекта недоступен',
  paused: 'доставка на паузе', not_received: 'не получено', running_or_lost: 'процесс не подтверждён',
};
export const turnErrorText = (code: string | null | undefined): string =>
  !code ? '' : TURN_ERROR_TEXT[code] ?? (code.startsWith('provider_') ? 'ошибка провайдера' : code);

export function nameOf(info: GroupStatus | null, agentId: string | null | undefined): string {
  if (!agentId) return '—';
  if (agentId === 'owner') return 'Owner';
  return info?.names[agentId] ?? `агент ${agentId.slice(0, 8)}`;
}

/** Plain-language state of the room; animation only for provider-confirmed activity. */
function roomState(info: GroupStatus): { tone: 'idle' | 'active' | 'warning' | 'danger'; text: string; live: boolean } {
  const turn = info.holding_turn;
  const who = nameOf(info, turn?.agent_id);
  if (info.controls.emergency_brake) return { tone: 'danger', text: 'Emergency Brake — новые ходы запрещены', live: false };
  if (turn?.state === 'unknown') return { tone: 'danger', text: `UNKNOWN: ход ${who} не подтверждён — нужна сверка`, live: false };
  if (info.controls.paused) return { tone: 'warning', text: 'Доставка на паузе', live: false };
  if (turn?.state === 'submitted') return info.activity === 'cli_confirmed'
    ? { tone: 'active', text: `${who}: CLI подтвердил сессию, формирует ответ`, live: true }
    : { tone: 'active', text: `${who}: запущено, ожидает подтверждения CLI`, live: false };
  if (turn?.state === 'reserved') return { tone: 'active', text: `${who}: подготовка хода`, live: false };
  if (turn && (turn.state === 'accepted' || turn.state === 'reply_captured'))
    return { tone: 'active', text: `${who}: ответ получен, публикация`, live: false };
  if (info.catchup) return { tone: 'warning', text: `Нужно решение Owner: пропущенный контекст ${nameOf(info, info.catchup.agent_id)}`, live: false };
  if (info.round?.state === 'awaiting_owner') {
    // P052: a natural stop and an exhausted reply limit are different facts; neither claims a model was contacted.
    if (info.stop_reason === 'quota_exhausted') return { tone: 'warning', text: 'Лимит ответов исчерпан · последнее обращение по @имени осталось без ответа', live: false };
    if (info.stop_reason === 'unanswered') return { tone: 'warning', text: 'Обсуждение остановлено · последнее обращение по @имени осталось без ответа', live: false };
    // P052 r04: an Agent marked its answer final; the spent allowance is not reusable by Agents.
    if (info.stop_reason === 'finished') return { tone: 'idle', text: 'Обсуждение завершено финальным ответом агента · продолжит только новое сообщение Owner', live: false };
    return { tone: 'idle', text: 'Обсуждение завершилось само · следующее сообщение Owner откроет новое', live: false };
  }
  if (info.round && info.driver.running) return { tone: 'active', text: 'Агенты отвечают по очереди', live: false };
  if (info.round) return { tone: 'idle', text: 'Ответы разрешены · очередь ожидает', live: false };
  return { tone: 'idle', text: 'Нет активного обсуждения', live: false };
}

function Dots({ label }: { label: string }) {
  return <span className="rosc-group-dots" role="img" aria-label={label}><i /><i /><i /></span>;
}

/** P052: Core's reason an explicitly @addressed Agent got no turn (no model was contacted for it). */
const UNANSWERED_TEXT: Record<string, string> = {
  quota_exhausted: 'у него не осталось ответов в этом обсуждении',
  not_in_this_discussion: 'он не участник этого обсуждения — агенты не могут добавлять участников',
  reply_too_long: 'сообщение длиннее лимита пересылки агентам',
  paused: 'доставка на паузе', emergency_brake: 'включён Emergency Brake', awaiting_owner: 'обсуждение ждёт Owner',
  excluded_after_failures: 'исключён после двух безопасных отказов', released: 'освобождён Owner',
  unassigned: 'не назначен этой комнате', transport_unknown: 'транспорт недоступен',
  // P052 r03: why an Architect's @invitation could not open a discussion (recorded at reply commit time).
  newer_owner_message: 'приглашение не открыло обсуждение: Owner уже написал новое сообщение',
  pending_owner_request: 'приглашение не открыло обсуждение: незавершённый запрос Owner в этой комнате не прерывается',
  peer_unavailable: 'адресат сейчас не может ответить',
  // P052 r04: the Owner authorization behind this request was already used; only a new Owner message or Renew refills it.
  authorization_spent: 'разрешение Owner на обсуждение уже израсходовано — нужно новое сообщение Owner',
  finish_requested: 'агент завершил обсуждение финальным ответом',
  ...UNSUPPORTED_TEXT,
};

/** P052: "@X was addressed but did not answer" lines + the Owner-only continuation when a reply limit was the cause. */
function UnansweredNotes({ group, info }: { group: GroupControls; info: GroupStatus }) {
  const [problem, setProblem] = useState('');
  const items = info.unanswered ?? [];
  if (items.length === 0) return null;
  const canContinue = info.round?.state === 'awaiting_owner' && items.some(u => u.reason === 'quota_exhausted');
  return <div className="rosc-group-note rosc-group-note--warning" role="status">
    {items.map(u => <p key={`${u.message_id}-${u.agent_id}`}>
      #{u.ordinal}: {nameOf(info, u.from_agent_id)} → @{nameOf(info, u.agent_id)} — без ответа: {UNANSWERED_TEXT[u.reason] ?? u.reason}. Модель не вызывалась.</p>)}
    {canContinue && <button type="button" className="rosc-btn rosc-btn--small" disabled={group.busy !== null}
      onClick={async () => setProblem((await group.onContinue()) ?? '')}>Продолжить обсуждение</button>}
    {canContinue && <small> Откроет новое обсуждение (до {info.settings.group_reply_quota_default ?? 3} ответов на агента); первым ответит адресат последнего сообщения.</small>}
    {problem && <p className="rosc-conversation__error" role="alert">{problem}</p>}
  </div>;
}

/** Small accessible dialog: focus moves inside, Escape and the close button dismiss it. */
export function GroupDialog({ title, kicker, children, onClose }: { title: string; kicker?: string; children: ReactNode; onClose: () => void }) {
  const id = useId();
  const box = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const first = box.current?.querySelector<HTMLElement>('button:not([disabled])');
    first?.focus();
  }, []);
  return <div className="rosc-group-dialog__backdrop" onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); onClose(); } }}>
    <div className="rosc-group-dialog" role="dialog" aria-modal="true" aria-labelledby={id} ref={box}>
      <div className="rosc-group-dialog__head">
        <span>{kicker ?? 'ROSC'}</span>
        <button type="button" className="rosc-chat-icon" aria-label="Закрыть" onClick={onClose}>×</button>
      </div>
      <h4 id={id}>{title}</h4>
      {children}
    </div>
  </div>;
}


function QuotaCards({ info }: { info: GroupStatus }) {
  const round = info.round;
  if (!round) return null;
  return <div className="rosc-group-quota">
    {Object.entries(round.grants).map(([agentId, grant]) => {
      const current = info.holding_turn?.agent_id === agentId;
      return <div key={agentId} className={current ? 'rosc-group-quota__card rosc-group-quota__card--current' : 'rosc-group-quota__card'}>
        <div className="rosc-group-quota__top"><strong>{nameOf(info, agentId)}</strong><span>{grant.published}/{grant.quota}</span></div>
        <progress className="rosc-group-quota__bar" value={Math.min(grant.published, grant.quota)} max={Math.max(1, grant.quota)} aria-hidden="true" />
        <small>{grant.failed ? 'исключён: два безопасных отказа' : current ? 'текущий ход'
          : grant.published >= grant.quota ? 'квота исчерпана' : `осталось ${grant.quota - grant.published}`}</small>
      </div>;
    })}
  </div>;
}

/** P047: the room is in a TRUE group interaction (Owner + 2+ addressed Agents, or Agent -> Agent with the Owner observing).
 * P052 r03: only while that interaction is still RUNNING; a finished round (natural stop, limit reached) is a past discussion, not an active one. */
export const groupEngaged = (info: GroupStatus | null): boolean =>
  info?.engagement === 'group' && (info.round_active ?? info.round?.state !== 'awaiting_owner');

/** Critical states that must stay visible and recoverable even in ordinary 1:1 chatting. */
export function groupAttention(info: GroupStatus | null): boolean {
  if (!info) return false;
  return info.controls.emergency_brake || info.controls.paused || info.holding_turn?.state === 'unknown'
    || info.failed.length > 0 || info.catchup !== null || (info.unanswered?.length ?? 0) > 0;
}

/**
 * P047 group header. ABSENT in direct 1:1 chatting (no reserved height). A compact strip appears only while a true
 * group interaction is relevant, with expandable quota / participants / pause / brake details. Critical states
 * (Brake, Pause, UNKNOWN, failed turns) get a narrow alert strip even in 1:1. There is no Authorize / Start / Renew
 * step: the Owner's Send is the authorization (default N from ROSC Settings).
 */
export function GroupBar({ group, open, onOpenChange }: { group: GroupControls; open: boolean; onOpenChange: (open: boolean) => void }) {
  const info = group.info;
  const [problem, setProblem] = useState('');
  /** The held turn an Owner reconciliation already ran for (so the outcome can be reported, never invented). */
  const [reconciled, setReconciled] = useState<string | null>(null);
  const run = async (action: () => Promise<string | null>) => setProblem((await action()) ?? '');
  if (group.status === 'failed' && !info) return <div className="rosc-group-strip rosc-group-strip--danger" role="alert">
    Состояние разговора недоступно: {group.error} <button type="button" className="rosc-btn rosc-btn--small" onClick={group.onReload}>Повторить</button>
  </div>;
  if (!info) return null;
  const engaged = groupEngaged(info);
  const attention = groupAttention(info);
  if (!engaged && !attention) return null;
  const state = roomState(info);
  const round = info.round;
  const busy = group.busy !== null;
  const unknown = info.holding_turn?.state === 'unknown' ? info.holding_turn : null;
  const controls = <>
    <button type="button" className={info.controls.paused ? 'rosc-btn rosc-btn--small rosc-btn--on rosc-group-ctl' : 'rosc-btn rosc-btn--small rosc-group-ctl'}
      aria-pressed={info.controls.paused} disabled={busy || !info.conversation_id}
      onClick={() => run(() => group.onControls({ paused: !info.controls.paused }))}>
      {info.controls.paused ? 'Снять паузу' : 'Пауза'}</button>
    <button type="button" className={info.controls.emergency_brake ? 'rosc-btn rosc-btn--small rosc-btn--danger-on rosc-group-ctl' : 'rosc-btn rosc-btn--small rosc-group-ctl'}
      aria-pressed={info.controls.emergency_brake} disabled={busy || !info.conversation_id}
      onClick={() => run(() => group.onControls({ emergency_brake: !info.controls.emergency_brake }))}>
      {info.controls.emergency_brake ? 'Снять Brake' : 'Brake'}</button>
  </>;
  const unknownBanner = unknown && <UnknownBanner turn={unknown} info={info} busy={busy}
    running={info.driver.running} checked={reconciled === unknown.turn_id}
    onReconcile={async () => {
      const id = unknown.turn_id;
      const failure = await group.onReconcile();
      setProblem(failure ?? '');
      if (!failure) setReconciled(id);
    }} />;
  const failedNotes = info.failed.map(turn => <p key={turn.turn_id} className="rosc-group-note rosc-group-note--warning">
    {nameOf(info, turn.agent_id)}: ход не принят дважды ({turnErrorText(turn.error_code)}) — агент исключён из этого обсуждения.</p>);
  const errorLine = (problem || (group.error && group.status === 'failed'))
    && <p className="rosc-conversation__error" role="alert">{problem || group.error}</p>;

  if (!engaged) {
    // Ordinary 1:1 chatting with a critical state: a narrow strip, never the full group panel.
    return <section className={`rosc-group-strip rosc-group-strip--${state.tone}`} aria-label="Состояние доставки">
      <div className="rosc-group-strip__row">
        <span className={`rosc-group-pill rosc-group-pill--${state.tone}`} aria-live="polite">{state.text}</span>
        <span className="rosc-group-strip__actions">{controls}</span>
      </div>
      {unknownBanner}
      {failedNotes}
      <UnansweredNotes group={group} info={info} />
      {errorLine}
    </section>;
  }

  const agents = round ? Object.keys(round.grants) : info.members.filter(m => m.state === 'active').map(m => m.agent_id);
  return <section className={`rosc-group-bar rosc-group-bar--${state.tone}`} aria-label="Групповой чат">
    <div className="rosc-group-bar__head">
      <button type="button" className="rosc-group-toggle" aria-expanded={open} onClick={() => onOpenChange(!open)}>
        <span aria-hidden="true">{open ? '▾' : '▸'}</span> Группа: Owner{agents.map(a => `, ${nameOf(info, a)}`).join('')}
      </button>
      <span className="rosc-group-bar__n" title="Потолок ответов каждого агента на одно сообщение Owner (настройка ROSC)">
        до <strong>{round?.per_agent_quota ?? info.settings.group_reply_quota_default ?? 3}</strong> на агента
      </span>
      <span className="rosc-group-bar__ctl">{controls}</span>
    </div>
    <p className={`rosc-group-pill rosc-group-pill--${state.tone}`} aria-live="polite">
      {state.text}{state.live && <Dots label="формирует ответ" />}
    </p>
    {unknownBanner}
    {failedNotes}
    <UnansweredNotes group={group} info={info} />
    {open && <div className="rosc-group-bar__body">
      {round && <>
        <p className="rosc-group-line">Обсуждение #{round.round_no} · каждое новое групповое сообщение Owner открывает новое обсуждение</p>
        <p className="rosc-group-line">Сейчас: <span className="rosc-group-tag">{info.holding_turn ? nameOf(info, info.holding_turn.agent_id) : 'никто не отвечает'}</span>
          {' '}Далее: {info.waiting.length ? info.waiting.map((a, i) => <span key={`${a}-${i}`} className="rosc-group-tag">{nameOf(info, a)}</span>)
            : <span className="rosc-muted">только если агента позовут по @имени</span>}</p>
        <QuotaCards info={info} />
      </>}
      <GroupParticipants group={group} open onOpenChange={() => undefined} embedded />
      <p className="rosc-group-hint">Отправка сообщения двум и более агентам сама разрешает ответы: каждый отвечает один раз, дальше — только если коллега обратился к нему по @имени, не больше N. Одна генерация на комнату за раз. Пауза и Brake запрещают новые ходы, но не отменяют уже запущенный.</p>
    </div>}
    {errorLine}
  </section>;
}

function UnknownBanner({ turn, info, busy, running, checked, onReconcile }: { turn: GroupTurn; info: GroupStatus; busy: boolean; running: boolean; checked: boolean; onReconcile: () => void }) {
  return <div className="rosc-group-note rosc-group-note--danger" role="alert">
    <p><strong>UNKNOWN:</strong> принятие хода {nameOf(info, turn.agent_id)} не подтверждено ({turnErrorText(turn.error_code) || 'нет доказательств'}). Комната удержана, повторной отправки нет, квота не списана.</p>
    {checked && <p>Сверка выполнена: доказательств принятия или отказа нет — ход остаётся UNKNOWN. Автоматического повтора не будет.</p>}
    <button type="button" className="rosc-btn rosc-btn--small" disabled={busy || running} onClick={onReconcile}>Запросить сверку</button>
    <small>Сверка только читает evidence CLI. Если доказательств нет, ход остаётся UNKNOWN.</small>
  </div>;
}

/** Collapsible roster (r05 «Участники»): Active / Passive / Released, missed count, Owner-only Release. */
export function GroupParticipants({ group, open, onOpenChange, embedded = false }: { group: GroupControls; open: boolean; onOpenChange: (open: boolean) => void; embedded?: boolean }) {
  const info = group.info;
  const [confirm, setConfirm] = useState<string | null>(null);
  const [problem, setProblem] = useState('');
  if (!info) return null;
  const members = new Map(info.members.map(m => [m.agent_id, m]));
  const count = (state: string) => info.members.filter(m => m.state === state).length;
  return <section className="rosc-group-people" aria-label="Участники">
    {embedded ? <p className="rosc-group-line">Участники: <small>active {count('active')} · passive {count('passive')} · released {count('released')}</small></p>
      : <button type="button" className="rosc-group-toggle" aria-expanded={open} onClick={() => onOpenChange(!open)}>
        <span aria-hidden="true">{open ? '▾' : '▸'}</span> Участники: <small>active {count('active')} · passive {count('passive')} · released {count('released')}</small>
      </button>}
    {open && <div className="rosc-group-people__list">
      {info.roster.length === 0 && <p className="rosc-group-hint">В этой области нет назначенных агентов.</p>}
      {info.roster.map(entry => {
        const member = members.get(entry.agent_id);
        return <div key={entry.agent_id} className="rosc-group-person">
          <div className="rosc-group-person__top">
            <strong>{entry.display_name}</strong>
            <span className="rosc-group-role">{entry.role === 'architect' ? 'Architect' : 'Worker'}</span>
            {member ? <span className={`rosc-group-badge rosc-group-badge--${member.state}`}>{member.state === 'active' ? 'Active' : member.state === 'passive' ? 'Passive' : 'Released'}</span>
              : <span className="rosc-group-badge">не в комнате</span>}
          </div>
          <small>{member?.state === 'passive' ? `пропущено ${member.missed}` : member?.state === 'active' ? 'в диалоге' : member ? 'не получает контекст группы' : 'присоединится при первом обращении, без истории до входа'}
            {member && member.omitted_ranges.length > 0 && ` · осознанный пропуск ${member.omitted_ranges.map(([a, b]) => `#${a}–#${b}`).join(', ')}`}</small>
          {!entry.group_supported && <small className="rosc-group-unsupported">{UNSUPPORTED_TEXT[entry.unsupported_reason ?? ''] ?? entry.unsupported_reason}</small>}
          {member && member.state !== 'released' && <button type="button" className="rosc-link-btn" disabled={group.busy !== null}
            onClick={() => setConfirm(entry.agent_id)}>Освободить</button>}
        </div>;
      })}
      <p className="rosc-group-hint">Passive видит беседу, но модель не вызывается. Released — только по решению Owner.</p>
    </div>}
    {problem && <p className="rosc-conversation__error" role="alert">{problem}</p>}
    {confirm && <GroupDialog title={`Освободить ${nameOf(info, confirm)} от обсуждения?`} onClose={() => setConfirm(null)}>
      <p>Участник перестанет получать сообщения этой группы. При повторном обращении ROSC не передаёт ему историю за время Released. Сообщения в канонической истории не удаляются.</p>
      <div className="rosc-group-dialog__actions">
        <button type="button" className="rosc-btn rosc-btn--primary" onClick={async () => {
          const id = confirm; setConfirm(null); setProblem((await group.onRelease(id)) ?? '');
        }}>Перевести в Released</button>
        <button type="button" className="rosc-btn" onClick={() => setConfirm(null)}>Оставить в обсуждении</button>
      </div>
    </GroupDialog>}
  </section>;
}

/** Non-chat notices (never canonical messages): ROSC suggests releasing a long-Passive member. */
export function ReleaseOffers({ group }: { group: GroupControls }) {
  const info = group.info;
  const [problem, setProblem] = useState('');
  if (!info) return null;
  const offers = info.members.filter(m => m.release_offer);
  if (offers.length === 0 && !problem) return null;
  return <div className="rosc-group-offers">
    {offers.map(member => <div key={member.agent_id} className="rosc-group-offer" role="status">
      <strong>ROSC предлагает освободить {nameOf(info, member.agent_id)}</strong>
      <p>Участник Passive пропустил {member.release_offer!.missed} сообщений (порог {member.release_offer!.threshold} из настроек ROSC). Он остаётся в беседе, пока вы не решите иначе. Это уведомление, не сообщение чата.</p>
      <div className="rosc-group-dialog__actions">
        <button type="button" className="rosc-btn rosc-btn--small" disabled={group.busy !== null}
          onClick={async () => setProblem((await group.onKeep(member.agent_id)) ?? '')}>Оставить в группе</button>
        <button type="button" className="rosc-btn rosc-btn--small rosc-btn--primary" disabled={group.busy !== null}
          onClick={async () => setProblem((await group.onRelease(member.agent_id)) ?? '')}>Перевести в Released</button>
      </div>
    </div>)}
    {problem && <p className="rosc-conversation__error" role="alert">{problem}</p>}
  </div>;
}

/** r05 «Передать пропущенный контекст?»: a real Core decision with the real count and character size. */
export function GroupCatchupDialog({ group }: { group: GroupControls }) {
  const info = group.info;
  const [dismissed, setDismissed] = useState<string | null>(null);
  const [problem, setProblem] = useState('');
  const catchup = info?.catchup ?? null;
  if (!info || !catchup) return null;
  const choose = async (choice: 'send_full' | 'skip_backlog') => setProblem((await group.onCatchup(catchup.turn_id, choice)) ?? '');
  if (dismissed === catchup.turn_id) return <div className="rosc-group-offer" role="status">
    <strong>Нужно решение по пропущенному контексту {nameOf(info, catchup.agent_id)}</strong>
    <button type="button" className="rosc-btn rosc-btn--small" onClick={() => setDismissed(null)}>Решить</button>
  </div>;
  return <GroupDialog title="Передать пропущенный контекст?" onClose={() => setDismissed(catchup.turn_id)}>
    <p>Перед ходом нужно синхронизировать нативную сессию получателя. Размер пропущенной истории превышает порог {catchup.limit} символов из настроек ROSC.</p>
    <p className="rosc-group-catchup__facts"><strong>{nameOf(info, catchup.agent_id)}</strong> · {catchup.count} сообщений · {catchup.chars} символов · #{catchup.from}–#{catchup.through}</p>
    <p><strong>Передать:</strong> добавить пропущенные сообщения перед текущим вопросом. <strong>Пропустить:</strong> отправить только новое; пропуск фиксируется явно, история ROSC не удаляется и не считается принятой агентом.</p>
    <div className="rosc-group-dialog__actions">
      <button type="button" className="rosc-btn rosc-btn--primary" disabled={group.busy !== null} onClick={() => choose('send_full')}>Передать историю</button>
      <button type="button" className="rosc-btn" disabled={group.busy !== null} onClick={() => choose('skip_backlog')}>Пропустить историю</button>
      <button type="button" className="rosc-link-btn" onClick={() => setDismissed(catchup.turn_id)}>Отмена</button>
    </div>
    <small>Решение продолжает уже разрешённую очередь этой комнаты.</small>
    {problem && <p className="rosc-conversation__error" role="alert">{problem}</p>}
  </GroupDialog>;
}

/** Transient, non-canonical row at the end of the feed while a turn holds the room. */
export function GroupActivityRow({ info }: { info: GroupStatus | null }) {
  const turn = info?.holding_turn;
  if (!info || !turn || !['reserved', 'submitted', 'accepted', 'reply_captured'].includes(turn.state)) return null;
  const confirmed = turn.state === 'submitted' && info.activity === 'cli_confirmed';
  return <div className="rosc-group-activity" aria-live="polite">
    <strong>{nameOf(info, turn.agent_id)}</strong>
    <span>{confirmed ? '· формирует ответ' : turn.state === 'submitted' ? '· ожидает подтверждения CLI' : turn.state === 'reserved' ? '· подготовка хода' : '· публикация ответа'}</span>
    {confirmed && <Dots label="формирует ответ" />}
  </div>;
}

export const ARCHITECT_CHIP = 'architect';

/** Composer addressees: Architect + every in-scope Worker; unsupported ones disabled. Selection is never changed silently. */
export function RecipientChips({ group, selected, onToggle, disabled, architect }: {
  group: GroupControls; selected: ReadonlySet<string>; onToggle: (id: string) => void; disabled: boolean;
  architect: { label: string; available: boolean; title?: string };
}) {
  const workers = (group.info?.roster ?? []).filter(entry => entry.role === 'worker');
  // P047: the selection is shown exactly as made; an unavailable Agent stays visible but cannot be chosen.
  const chip = (id: string, label: string, role: string, enabled: boolean, title?: string) => {
    const on = selected.has(id);
    return <label key={id} className={`rosc-recipient${on ? ' rosc-recipient--on' : ''}${enabled ? '' : ' rosc-recipient--off'}`} title={title}>
      <input type="checkbox" aria-label={`Адресовать ${label}`} checked={on} disabled={disabled || !enabled}
        onChange={() => onToggle(id)} />
      <span>@{label}</span><small>{role}</small>
    </label>;
  };
  return <div className="rosc-recipients" role="group" aria-label="Кому направить сообщение">
    {chip(ARCHITECT_CHIP, architect.label, 'Architect', architect.available, architect.title)}
    {workers.map(entry => chip(entry.agent_id, entry.display_name, 'Worker', entry.group_supported,
      entry.group_supported ? undefined : UNSUPPORTED_TEXT[entry.unsupported_reason ?? ''] ?? entry.unsupported_reason ?? undefined))}
    {group.status === 'loading' && <small className="rosc-muted">Загрузка участников…</small>}
  </div>;
}

/** Address line + stable copyable `#ordinal` for one message bubble. */
export function MessageAddress({ message, info, refPrefix }: { message: ConversationMessage; info: GroupStatus | null; refPrefix: string }) {
  const [copied, setCopied] = useState(false);
  const to = message.group?.to ?? (message.recipient === 'architect' ? ['Architect'] : []);
  const label = to.map(id => id === 'Architect' ? '@Architect' : id === 'owner' ? '@Owner' : `@${nameOf(info, id)}`).join(', ');
  const reference = `${refPrefix}#${message.ordinal}`;
  return <>
    {label && <span className="rosc-chat-to">→ {label}</span>}
    {message.group?.reply_to_ordinal ? <span className="rosc-chat-to">↩ #{message.group.reply_to_ordinal}</span> : null}
    {message.group?.next === 'finish' ? <span className="rosc-chat-to" title="Агент завершил обсуждение">✓ финальный ответ</span> : null}
    <button type="button" className="rosc-chat-ref" aria-label={`Скопировать ссылку на сообщение ${message.ordinal}`}
      title={`Скопировать ссылку ${reference}`}
      onClick={async () => {
        try { await navigator.clipboard?.writeText(reference); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { setCopied(false); }
      }}>{copied ? 'скопировано' : `#${message.ordinal}`}</button>
  </>;
}

/** P055: the managed Run handoff is a SERVICE message (durable receipt): no discussion, no model call for it. */
const HANDOFF_TEXT: Record<string, string> = {
  awaiting_architect_review: 'Отчёт Worker получен — ожидает Architect Review',
  review_passed: 'Отчёт Worker · Architect Review пройден (не VERIFIED, не принято Owner) — решение за Owner',
  changes_requested: 'Отчёт Worker · Architect запросил изменения — нужно решение Owner',
  inconclusive: 'Отчёт Worker · Review incomplete / needs evidence',
};
export function runHandoffNote(info: GroupStatus | null, messageId: string): string | null {
  const handoff = info?.run_handoffs?.find(item => item.message_id === messageId);
  if (!handoff) return null;
  return `${HANDOFF_TEXT[handoff.review_state] ?? 'Отчёт Worker'} · служебное сообщение: обсуждение не открывалось, модель не вызывалась.`;
}

/** Display name for a message sender: the registered Agent name for Worker replies. */
export function senderName(message: ConversationMessage, info: GroupStatus | null): string {
  if (message.sender.kind === 'worker') return message.sender.agent_display_name ?? nameOf(info, message.sender.agent_id);
  return message.sender.display_name;
}

/**
 * P052: addressees the Owner typed as `@DisplayName` (preview; Core re-parses). Returns chip ids (the Architect as
 * `ARCHITECT_CHIP`), whether `@Owner` was typed, ambiguous and unknown names (P052 r02: an unknown explicit `@name` is
 * refused, never silently sent to the Architect), and typed names of Agents in this room that cannot answer now.
 */
export function typedAddressees(info: GroupStatus | null, text: string):
  { ids: string[]; owner: boolean; ambiguous: string[]; unknown: string[]; unavailable: string[] } {
  const roster = info?.roster ?? [];
  const candidates: Record<string, string[]> = { owner: ['Owner'] };
  for (const entry of roster) {
    const id = entry.role === 'architect' ? ARCHITECT_CHIP : entry.agent_id;
    candidates[id] = entry.role === 'architect' && !roster.some(e => e.display_name.toLowerCase() === 'architect')
      ? [entry.display_name, 'Architect'] : [entry.display_name];
  }
  const found = findMentions(text, candidates);
  const ids = found.refs.filter(ref => ref !== 'owner');
  const unavailable = ids.map(id => roster.find(e => (e.role === 'architect' ? ARCHITECT_CHIP : e.agent_id) === id))
    .filter(e => e && e.role === 'worker' && !e.group_supported).map(e => e!.display_name);
  return { ids, owner: found.refs.includes('owner'), ambiguous: found.ambiguous, unknown: found.unknown, unavailable };
}

/** P052 r02: the one truthful problem with the typed addressees, or null. Shared by PC and TC. */
export function typedProblem(typed: ReturnType<typeof typedAddressees>): string | null {
  if (typed.ambiguous.length > 0) return `@${typed.ambiguous[0]} совпадает с несколькими агентами — выберите адресата кнопкой.`;
  if (typed.unknown.length > 0) {
    return `@${typed.unknown[0]} — нет такого участника в этой переписке. Проверьте имя или выберите адресата кнопкой; `
      + `чтобы написать «@» как текст, возьмите фрагмент в обратные кавычки.`;
  }
  if (typed.unavailable.length > 0) return `${typed.unavailable[0]}: ответы недоступны для этого агента.`;
  return null;
}

/**
 * P052 r02: explicit Agent ids for the room route = the Owner's CHIPS only (Workers, plus the assigned Architect when its
 * chip is on). Typed names are not sent as ids: Core parses the text authoritatively and unites them, so a preview that
 * diverges from Core can never address an Agent Core would not.
 */
export function chipTargets(info: GroupStatus | null, selected: ReadonlySet<string>): string[] {
  const to = [...selected].filter(id => id !== ARCHITECT_CHIP);
  const architect = info?.roster.find(entry => entry.role === 'architect');
  if (selected.has(ARCHITECT_CHIP) && architect) to.push(architect.agent_id);
  return to;
}

/** Chips united with typed `@names` (chips first, stable order); never removes a chip the Owner selected. */
export function effectiveSelection(selected: ReadonlySet<string>, typed: string[]): Set<string> {
  const merged = new Set(selected);
  for (const id of typed) merged.add(id);
  return merged;
}

/**
 * P047 composer routing, shared by PC and TC. Owner -> one Worker: a DIRECT reply; Owner -> two or more Agents (Workers,
 * optionally with the Architect): ONE group message. The Architect alone keeps its established direct path. Returns the
 * structured addressees for the group route, or a truthful reason the selection cannot be sent now.
 */
export function routeSelection(info: GroupStatus | null, selected: ReadonlySet<string>):
  { route: 'architect' | 'group'; to: string[]; problem: string | null } {
  const workers = [...selected].filter(id => id !== ARCHITECT_CHIP);
  if (workers.length === 0) return { route: 'architect', to: [], problem: null };
  const to = [...workers];
  if (selected.has(ARCHITECT_CHIP)) {
    const architect = info?.roster.find(entry => entry.role === 'architect');
    if (!architect) return { route: 'group', to, problem: 'Architect не назначен этому проекту.' };
    if (!architect.group_supported) {
      return { route: 'group', to, problem: UNSUPPORTED_TEXT[architect.unsupported_reason ?? ''] ?? 'Architect сейчас недоступен для группового разговора.' };
    }
    to.push(architect.agent_id);
  }
  return { route: 'group', to, problem: null };
}
