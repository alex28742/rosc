import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import type { GroupControls } from '../../../app/presentation/contracts';
import type { ConversationMessage, GroupStatus, GroupTurn } from '../../../core/models';

/**
 * P046 group conversation pieces shared by the Project (PC) and Task (TC) conversation panels. Everything rendered
 * here is Core-reported state; nothing is simulated. The r05 prototype's demo-only controls (synthetic publish/failure/
 * UNKNOWN, preset stages, session simulation, reset) are intentionally absent.
 */

export const UNSUPPORTED_TEXT: Record<string, string> = {
  architect_group_not_integrated: 'Групповые ходы Architect пока не интегрированы — отправьте ему отдельным сообщением',
  adapter_not_supported: 'У этого адаптера нет группового транспорта (в v1.0 — только Claude Code)',
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
  if (info.round?.state === 'awaiting_owner') return { tone: 'warning', text: 'Ждём Owner: квота раунда исчерпана', live: false };
  if (info.round && info.driver.running) return { tone: 'active', text: 'Очередь выполняется', live: false };
  if (info.round) return { tone: 'idle', text: 'Раунд разрешён · ход не запущен', live: false };
  return { tone: 'idle', text: 'Нет открытого раунда', live: false };
}

function Dots({ label }: { label: string }) {
  return <span className="rosc-group-dots" role="img" aria-label={label}><i /><i /><i /></span>;
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

const sameAgents = (a: string[], b: string[]) => a.length === b.length && a.every(id => b.includes(id));

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

/** Collapsible group header (r05 «Групповой чат»): round, quota, Renew, Pause, Brake, start and reconciliation. */
export function GroupBar({ group, open, onOpenChange }: { group: GroupControls; open: boolean; onOpenChange: (open: boolean) => void }) {
  const info = group.info;
  const [problem, setProblem] = useState('');
  const defaultQuota = info?.settings.group_reply_quota_default ?? 3;
  const [quota, setQuota] = useState<number | null>(null);
  /** The held turn an Owner reconciliation already ran for (so the outcome can be reported, never invented). */
  const [reconciled, setReconciled] = useState<string | null>(null);
  const n = quota ?? defaultQuota;
  const run = async (action: () => Promise<string | null>) => setProblem((await action()) ?? '');
  if (group.status === 'failed' && !info) return <div className="rosc-group-bar rosc-group-bar--failed" role="alert">
    Групповой чат недоступен: {group.error} <button type="button" className="rosc-btn rosc-btn--small" onClick={group.onReload}>Повторить</button>
  </div>;
  if (!info) return null;
  const state = roomState(info);
  const round = info.round;
  const supported = new Set(info.roster.filter(r => r.group_supported).map(r => r.agent_id));
  const activeSupported = info.members.filter(m => m.state === 'active' && supported.has(m.agent_id)).map(m => m.agent_id);
  const grantAgents = round ? Object.keys(round.grants).filter(a => supported.has(a)) : [];
  const held = info.holding_turn !== null;
  const busy = group.busy !== null;
  const totalPublished = round ? Object.values(round.grants).reduce((sum, g) => sum + g.published, 0) : 0;
  const totalQuota = round ? Object.values(round.grants).reduce((sum, g) => sum + g.quota, 0) : 0;
  const canStart = Boolean(round && round.state === 'open' && !held && !info.driver.running && !info.controls.paused
    && !info.controls.emergency_brake && !info.catchup);
  const quotaOptions = [1, 2, 3, 4, 5].includes(defaultQuota) ? [1, 2, 3, 4, 5] : [1, 2, 3, 4, 5, defaultQuota].sort((a, b) => a - b);
  const last = info.driver.last;
  const lastNote = !info.driver.running && last?.reason && ['driver_error'].includes(last.reason)
    ? 'Обработчик хода остановился с ошибкой; состояние комнаты сохранено в Core.' : '';

  return <section className={`rosc-group-bar rosc-group-bar--${state.tone}`} aria-label="Групповой чат">
    <div className="rosc-group-bar__head">
      <button type="button" className="rosc-group-toggle" aria-expanded={open} onClick={() => onOpenChange(!open)}>
        <span aria-hidden="true">{open ? '▾' : '▸'}</span> Групповой чат
      </button>
      <span className="rosc-group-bar__n" title="Разрешённых ответов на каждого агента в текущем раунде">
        ответов на агента <strong>{round?.per_agent_quota ?? '—'}</strong>
      </span>
      <button type="button" className="rosc-btn rosc-btn--small" disabled={!round || busy || grantAgents.length === 0}
        title="Новый раунд для тех же агентов: N ответов каждому"
        onClick={() => run(() => group.onAuthorize(grantAgents, n, true))}>Продлить текущих</button>
      <button type="button" className={info.controls.paused ? 'rosc-btn rosc-btn--small rosc-btn--on' : 'rosc-btn rosc-btn--small'}
        aria-pressed={info.controls.paused} disabled={busy || !info.conversation_id}
        onClick={() => run(() => group.onControls({ paused: !info.controls.paused }))}>
        {info.controls.paused ? 'Снять паузу' : 'Пауза доставки'}</button>
      <button type="button" className={info.controls.emergency_brake ? 'rosc-btn rosc-btn--small rosc-btn--danger-on' : 'rosc-btn rosc-btn--small'}
        aria-pressed={info.controls.emergency_brake} disabled={busy || !info.conversation_id}
        onClick={() => run(() => group.onControls({ emergency_brake: !info.controls.emergency_brake }))}>
        {info.controls.emergency_brake ? 'Снять Emergency Brake' : 'Emergency Brake'}</button>
    </div>
    <p className={`rosc-group-pill rosc-group-pill--${state.tone}`} aria-live="polite">
      {state.text}{state.live && <Dots label="формирует ответ" />}
    </p>
    {open && <div className="rosc-group-bar__body">
      {round ? <>
        <p className="rosc-group-line">Раунд #{round.round_no} · проход {round.current_pass + 1} · опубликовано {totalPublished}/{totalQuota} · <strong>сообщения Owner не тратят квоту</strong></p>
        <p className="rosc-group-line">Текущий: <span className="rosc-group-tag">{info.holding_turn ? nameOf(info, info.holding_turn.agent_id) : 'нет активного'}</span>
          {' '}Далее: {info.waiting.length ? info.waiting.map((a, i) => <span key={`${a}-${i}`} className="rosc-group-tag">{nameOf(info, a)}</span>)
            : <span className="rosc-muted">{round.state === 'awaiting_owner' ? 'квота исчерпана' : 'следующий проход или конец квоты'}</span>}</p>
        <QuotaCards info={info} />
      </> : <p className="rosc-group-line">{info.has_owner_message
        ? 'Модель вызывается только после явного разрешения раунда Owner.'
        : 'Сначала отправьте сообщение выбранным Workers, затем разрешите им N ответов.'}</p>}
      {info.failed.map(turn => <p key={turn.turn_id} className="rosc-group-note rosc-group-note--warning">
        {nameOf(info, turn.agent_id)}: ход не принят дважды ({turnErrorText(turn.error_code)}) — агент исключён из этого раунда.</p>)}
      {info.holding_turn?.state === 'unknown' && <UnknownBanner turn={info.holding_turn} info={info} busy={busy}
        running={info.driver.running} checked={reconciled === info.holding_turn.turn_id}
        onReconcile={async () => {
          const id = info.holding_turn?.turn_id ?? null;
          const failure = await group.onReconcile();
          setProblem(failure ?? '');
          if (!failure) setReconciled(id);
        }} />}
      <div className="rosc-group-actions">
        <label className="rosc-group-quota-select">Ответов на агента
          <select value={n} onChange={event => setQuota(Number(event.target.value))} disabled={busy}>
            {quotaOptions.map(value => <option key={value} value={value}>{value}</option>)}
          </select>
        </label>
        {!round && <button type="button" className="rosc-btn rosc-btn--small rosc-btn--primary"
          disabled={busy || activeSupported.length === 0}
          title={activeSupported.length === 0 ? 'Нет активных адресатов с поддерживаемым адаптером: отправьте им сообщение' : undefined}
          onClick={() => run(() => group.onAuthorize(activeSupported, n, false))}>
          Разрешить {n} {n === 1 ? 'ответ' : 'ответа'}{activeSupported.length ? `: ${activeSupported.map(a => nameOf(info, a)).join(', ')}` : ''}</button>}
        {round && activeSupported.length > 0 && !sameAgents(activeSupported, grantAgents) && <button type="button"
          className="rosc-btn rosc-btn--small" disabled={busy || held || info.driver.running}
          title="Закрывает текущий раунд и разрешает новый для тех, к кому вы обратились последним сообщением"
          onClick={() => run(() => group.onAuthorize(activeSupported, n, true))}>
          Новый раунд ({n}): {activeSupported.map(a => nameOf(info, a)).join(', ')}</button>}
        {round && <button type="button" className="rosc-btn rosc-btn--small rosc-btn--primary" disabled={!canStart || busy}
          title={canStart ? 'Запускает разрешённую очередь: по одному агенту за раз' : undefined}
          onClick={() => run(group.onStart)}>{info.driver.running ? 'Ход выполняется…' : 'Запустить ход'}</button>}
      </div>
      <p className="rosc-group-hint">Одна генерация на комнату за раз. Следующий агент получает опубликованные ответы и новые сообщения Owner только в своём очередном ходе. Пауза и Brake запрещают новые ходы, но не отменяют уже запущенный.</p>
      {lastNote && <p className="rosc-group-note rosc-group-note--warning">{lastNote}</p>}
    </div>}
    {(problem || (group.error && group.status === 'failed')) && <p className="rosc-conversation__error" role="alert">{problem || group.error}</p>}
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
export function GroupParticipants({ group, open, onOpenChange }: { group: GroupControls; open: boolean; onOpenChange: (open: boolean) => void }) {
  const info = group.info;
  const [confirm, setConfirm] = useState<string | null>(null);
  const [problem, setProblem] = useState('');
  if (!info) return null;
  const members = new Map(info.members.map(m => [m.agent_id, m]));
  const count = (state: string) => info.members.filter(m => m.state === state).length;
  return <section className="rosc-group-people" aria-label="Участники">
    <button type="button" className="rosc-group-toggle" aria-expanded={open} onClick={() => onOpenChange(!open)}>
      <span aria-hidden="true">{open ? '▾' : '▸'}</span> Участники: <small>active {count('active')} · passive {count('passive')} · released {count('released')}</small>
    </button>
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
    <small>Решение запускает уже разрешённую очередь этой комнаты.</small>
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

/** Composer addressees: Architect (existing delivery path) + every in-scope Worker; unsupported ones disabled. */
export function RecipientChips({ group, selected, onToggle, disabled, architect }: {
  group: GroupControls; selected: ReadonlySet<string>; onToggle: (id: string) => void; disabled: boolean;
  architect: { label: string; available: boolean; title?: string };
}) {
  const workers = (group.info?.roster ?? []).filter(entry => entry.role === 'worker');
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
    <button type="button" className="rosc-chat-ref" aria-label={`Скопировать ссылку на сообщение ${message.ordinal}`}
      title={`Скопировать ссылку ${reference}`}
      onClick={async () => {
        try { await navigator.clipboard?.writeText(reference); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { setCopied(false); }
      }}>{copied ? 'скопировано' : `#${message.ordinal}`}</button>
  </>;
}

/** Display name for a message sender: the registered Agent name for Worker replies. */
export function senderName(message: ConversationMessage, info: GroupStatus | null): string {
  if (message.sender.kind === 'worker') return message.sender.agent_display_name ?? nameOf(info, message.sender.agent_id);
  return message.sender.display_name;
}
