import { useState } from 'react';
import type { ConversationControls, WorkspaceControls } from '../../../app/presentation/contracts';
import type { Project } from '../../../core/models';
import { ContextAccessSummary } from './ContextAccessSummary';
import { WorkspaceActions } from './WorkspaceActions';
import { describeArchitect } from './architectView';

const MODE_LABEL = { new: 'новый проект', existing: 'существующий проект' } as const;

type Props = {
  project: Project;
  conversation: ConversationControls;
  workspace: WorkspaceControls;
  onOpenSettings: () => void;
};

/**
 * Approved Project overview card. It states the real Architect session, the Core Worker placeholder and the Project
 * mode, and owns the explicit Architect lifecycle actions. Opening the card, or the Project, never starts a runtime.
 */
export function ProjectAgentsCard({ project, conversation, workspace, onOpenSettings }: Props) {
  const architect = conversation.architect;
  const [failure, setFailure] = useState('');
  const view = architect.status === 'failed'
    ? { state: 'статус не проверен', label: 'Architect: статус не проверен', live: false,
      tone: 'error' as const, hint: '' }
    : describeArchitect(architect.info, architect.status === 'ready', architect.busy);
  const workers = project.active_workers;

  return <section className="rosc-panel-card rosc-agents-card" aria-label="Агенты проекта">
    <h3>Агенты проекта</h3>
    <div className="rosc-agent-stat">
      <span>Architect</span>
      <strong className={`rosc-agent-state rosc-agent-state--${view.tone}`} aria-live="polite">{view.state}</strong>
    </div>
    <div className="rosc-agent-stat">
      <span>Workers в работе</span>
      <strong className={workers > 0 ? 'rosc-agent-state rosc-agent-state--on' : 'rosc-agent-state rosc-agent-state--off'}>
        {workers}
      </strong>
    </div>
    <div className="rosc-agent-stat">
      <span>Режим</span>
      <strong className="rosc-agent-state rosc-agent-state--neutral">{MODE_LABEL[project.mode]}</strong>
    </div>
    <div className="rosc-agents-card__actions">
      {architect.info?.can_activate && !architect.busy && <button type="button" className="rosc-btn rosc-btn--small"
        onClick={async () => setFailure(await conversation.onActivate() ?? '')}>
        {architect.info.state === 'error' ? 'Повторить активацию Architect' : 'Активировать Architect'}
      </button>}
      {architect.info?.can_deactivate && !architect.busy && <button type="button" className="rosc-btn rosc-btn--small"
        onClick={async () => setFailure(await conversation.onDeactivate() ?? '')}>Остановить Architect</button>}
      {architect.status === 'failed' && <button type="button" className="rosc-btn rosc-btn--small"
        onClick={conversation.onRefreshArchitect}>Проверить статус</button>}
    </div>
    {(view.hint || architect.error || failure) && <p className="rosc-agents-card__hint" role="alert">
      {[view.hint, architect.error, failure].filter(Boolean).join(' ')}
    </p>}
    <ContextAccessSummary contextAccess={conversation.contextAccess} />
    <div className="rosc-agents-card__workspace">
      <strong className="rosc-agents-card__label">Workspace</strong>
      {/* Read-only Core fact: a healthy Workspace has no action, only the state it reports. */}
      {project.workspace_state === 'ready'
        ? <div className="rosc-repair"><p>Манифест Workspace соответствует записи в базе.</p></div>
        : <WorkspaceActions key={`${project.id}:${project.workspace_problem}`} state={project.workspace_state}
          problem={project.workspace_problem} controls={workspace} />}
    </div>
    <button type="button" className="rosc-agents-card__link" onClick={onOpenSettings}>
      Подробнее о назначениях — в настройках проекта
    </button>
  </section>;
}
