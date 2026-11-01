import type { DashboardViewProps } from '../../../app/presentation/contracts';
import { Feedback } from '../ui/Feedback';
import { ArrowUpRightIcon, AlertIcon } from '../ui/icons';

const MODE_LABEL = { new: 'новый каталог', existing: 'подключённый проект' } as const;

export function DashboardView({ projects, projectsStatus, feedback, inert, onOpenProject, onRegisterProject, onRetry }: DashboardViewProps) {
  const ready = projectsStatus === 'ready';
  return <main className="rosc-dashboard dashboard" inert={inert}>
    <div className="rosc-heading">
      <div>
        <p className="rosc-eyebrow">ПРОЕКТЫ</p>
        <h1 className="rosc-heading__title">Рабочее пространство</h1>
        <p className="rosc-heading__lead">Проекты и задачи доступны без запуска Architect или Worker.</p>
      </div>
      <div className="rosc-heading__actions">
        <button className="rosc-btn rosc-btn--primary" onClick={onRegisterProject}>＋ Добавить проект</button>
      </div>
    </div>
    <Feedback {...feedback} />
    <div className="rosc-project-toolbar">
      <div className="rosc-section-heading"><h2>Все проекты</h2>{ready && <span className="rosc-counter">{projects.length}</span>}</div>
    </div>
    {projectsStatus === 'failed' && <div className="rosc-state rosc-state--failed">
      <p><AlertIcon size="sm" /> Не удалось загрузить проекты. <button className="rosc-btn rosc-btn--small" onClick={onRetry}>Повторить</button></p>
    </div>}
    {projects.length === 0
      ? projectsStatus === 'loading'
        ? <div className="rosc-state rosc-state--loading"><span className="rosc-spinner" aria-hidden="true" />Загрузка проектов…</div>
        : ready && <section className="rosc-state">
          <p className="rosc-state__title">Проектов пока нет</p>
          <p>Подключите существующий проект или зарегистрируйте новый пустой каталог. ROSC хранит свои файлы отдельно от исходников проекта.</p>
        </section>
      : <div className="rosc-card-grid" aria-label="Проекты">{projects.map(p => <button className="rosc-project-card" key={p.id} onClick={() => onOpenProject(p.id)}>
        <span className="rosc-project-card__head">
          <span className="rosc-project-card__titles">
            <span className="rosc-project-card__name">{p.name}</span>
            <span className="rosc-project-card__subline">{p.id} · {MODE_LABEL[p.mode]}</span>
          </span>
          <span className={p.workspace_state === 'ready' ? 'rosc-project-card__status rosc-project-card__status--done' : 'rosc-project-card__status rosc-project-card__status--attention'}>
            {p.workspace_state === 'ready' ? 'Workspace готов' : 'Нужна сверка Workspace'}
          </span>
        </span>
        <span className="rosc-project-card__briefs">
          <span className="rosc-brief"><span className="rosc-brief__label">ОПИСАНИЕ</span><span className="rosc-brief__text">{p.description}</span></span>
          <span className="rosc-brief"><span className="rosc-brief__label">КОРНЕВАЯ ДИРЕКТОРИЯ</span><span className="rosc-brief__text rosc-brief__text--path">{p.root_path}</span></span>
          <span className="rosc-brief"><span className="rosc-brief__label">АГЕНТЫ</span><span className="rosc-brief__text">Architect: не активен · Workers: {p.active_workers}</span></span>
        </span>
        <span className="rosc-project-card__foot">
          <span className="rosc-project-card__meta">{p.docs_entrypoint ? `Документация: ${p.docs_entrypoint}` : 'Документация проекта не указана'}</span>
          <span className="rosc-project-card__cta">Открыть проект <ArrowUpRightIcon size="sm" /></span>
        </span>
      </button>)}</div>}
  </main>;
}
