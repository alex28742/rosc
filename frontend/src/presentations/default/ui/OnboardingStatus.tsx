import { useState } from 'react';
import type { ConversationControls } from '../../../app/presentation/contracts';
import type { OnboardingStep, OnboardingStepState } from '../../../core/models';

const HEADLINE: Record<string, string> = {
  ready: 'подтверждено',
  initializing: 'выполняется…',
  blocked: 'не подтверждено',
  unknown: 'итог неизвестен',
  legacy_unverified: 'не проверялось (сессия создана до проверки знакомства)',
};

/** Human wording for Core onboarding codes; an unknown code is shown as-is rather than guessed at. */
const REASON: Record<string, string> = {
  missing_evidence: 'Architect сообщил о готовности, но ROSC не подтвердил все обязательные чтения.',
  reported_by_architect: 'Architect сам сообщил, что знакомство не завершено.',
  no_status_marker: 'В ответе Architect нет итоговой строки статуса.',
  interrupted: 'Знакомство прервано перезапуском ROSC; запрос не повторялся.',
  timeout: 'ROSC перестал ждать ответа; запрос не повторялся.',
  connection_lost: 'Связь с runtime прервалась во время запроса; запрос не повторялся.',
  in_progress: 'Провайдер ещё обрабатывает запрос знакомства.',
  lookup_uncertain: 'Провайдер не дал однозначного ответа о запросе знакомства.',
  lookup_failed: 'Не удалось проверить запрос знакомства у провайдера.',
  not_received: 'Запрос знакомства до провайдера не дошёл.',
  tools_unavailable: 'ROSC Tools не подключены к сессии.',
  tools_not_supported: 'Runtime не поддерживает ROSC Tools.',
  session_not_ready: 'Сессия Architect не подтверждена.',
  assignment_changed: 'Назначение Architect изменилось.',
  completing: 'Owner запросил завершение знакомства.',
};

const STEP_LABEL: Record<string, string> = {
  briefing: 'Сводка проекта',
  role: 'Правила роли',
  onboarding_rules: 'Протокол знакомства',
  docs_entrypoint: 'Точка входа документации',
};

const STEP_STATE: Record<OnboardingStepState, string> = {
  done: 'прочитано',
  failed: 'ошибка чтения',
  missing: 'не подтверждено',
  pending: 'выполняется',
  unknown: 'исход неизвестен',
  not_verified: 'не проверялось',
};

const CHECK_RESULT: Record<string, string> = {
  busy: 'Сейчас идёт ход Architect — показаны сохранённые данные.',
  session_not_ready: 'Сессия Architect не подтверждена — проверка у провайдера не выполнялась.',
  session_not_verified: 'Сессию не удалось проверить — проверка у провайдера не выполнялась.',
  nothing_recorded: 'Для этой сессии ROSC не записывал запрос знакомства.',
  in_progress: 'Знакомство ещё выполняется.',
};

const reason = (code: string | null) => (code ? REASON[code] ?? `Код: ${code}` : '');
const stepText = (step: OnboardingStep) =>
  `${STEP_LABEL[step.id] ?? step.id}${step.path ? ` (${step.path})` : ''} — ${STEP_STATE[step.state] ?? step.state}`
  + (step.state === 'failed' && step.code ? `: ${step.code}` : '');

type Props = Pick<ConversationControls, 'architect' | 'onCheckOnboarding' | 'onCompleteOnboarding'>;

/**
 * P053 compact onboarding row inside the existing Project Architect block. It shows only what Core reports: the
 * verdict, its reason and each required read with the evidence state of the Tool audit. «Проверить» never asks the
 * model; «Завершить знакомство» is the only control that requests one extra short model turn, in the same session.
 */
export function OnboardingStatus({ architect, onCheckOnboarding, onCompleteOnboarding }: Props) {
  const [pending, setPending] = useState<'check' | 'complete' | null>(null);
  const [failure, setFailure] = useState('');
  const info = architect.info?.onboarding;
  if (!info || info.state === null || architect.info?.session?.state !== 'ready') return null;
  const steps = info.steps ?? [];
  const showSteps = info.state !== 'ready' || steps.some(step => step.state !== 'done');
  const checkNote = info.check ? CHECK_RESULT[info.check] ?? '' : '';
  const run = async (action: 'check' | 'complete') => {
    if (pending) return;
    setPending(action);
    setFailure('');
    const error = await (action === 'check' ? onCheckOnboarding() : onCompleteOnboarding());
    setPending(null);
    setFailure(error ?? '');
  };
  return <div className="rosc-onboarding" aria-label="Знакомство Architect с проектом">
    <p className="rosc-onboarding__state">
      <strong>Знакомство: {HEADLINE[info.state] ?? info.state}</strong>
      {info.state !== 'ready' && reason(info.detail) && <span> {reason(info.detail)}</span>}
    </p>
    {showSteps && steps.length > 0 && <ul className="rosc-onboarding__steps" aria-label="Обязательные чтения">
      {steps.map(step => <li key={step.id} className={`rosc-onboarding__step rosc-onboarding__step--${step.state}`}>
        {stepText(step)}</li>)}
    </ul>}
    {(info.can_check || info.can_complete) && <div className="rosc-agents-card__actions">
      {info.can_check && <button type="button" className="rosc-btn rosc-btn--small" disabled={pending !== null}
        onClick={() => void run('check')}>{pending === 'check' ? 'Проверка…' : 'Проверить'}</button>}
      {info.can_complete && info.state !== 'initializing' && <button type="button" className="rosc-btn rosc-btn--small"
        disabled={pending !== null} onClick={() => void run('complete')}>
        {pending === 'complete' ? 'Отправка…' : 'Завершить знакомство'}</button>}
    </div>}
    {info.can_complete && info.state !== 'initializing' && <p className="rosc-muted rosc-onboarding__note">
      «Завершить знакомство» отправит Architect один короткий запрос в той же сессии — только недостающие чтения.</p>}
    {(checkNote || failure) && <p className="rosc-agents-card__hint" role="alert">{[checkNote, failure].filter(Boolean).join(' ')}</p>}
  </div>;
}
