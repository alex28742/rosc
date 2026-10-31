import type { DashboardViewProps } from '../../../app/presentation/contracts';
import { Feedback } from '../ui/Feedback';

export function DashboardView({ projects, feedback, inert, onOpenProject, onRegisterProject }: DashboardViewProps) {
  return <main className="dashboard" inert={inert}>
    <div className="page-title">
      <div><p className="eyebrow">ПРОЕКТЫ</p><h1>Рабочее пространство</h1><p>Проекты и задачи доступны без запуска Architect или Worker.</p></div>
      <button className="primary" onClick={onRegisterProject}>＋ Добавить проект</button>
    </div>
    <Feedback {...feedback} />
    {projects.length === 0
      ? <section className="empty">Проектов пока нет. Подключите существующий или зарегистрируйте новый пустой каталог.</section>
      : <div className="cards">{projects.map(p => <button className="project-card" key={p.id} onClick={() => onOpenProject(p.id)}>
        <span className="id">{p.id}</span><h2>{p.name}</h2><p>{p.description}</p>
        <div className="card-foot"><span className="pill">Architect: не активен</span><span className="pill">Workers: 0</span><span className={p.workspace_state === 'ready' ? 'pill ok' : 'pill warning'}>{p.workspace_state === 'ready' ? 'Workspace готов' : 'Нужна сверка Workspace'}</span></div>
      </button>)}</div>}
  </main>;
}
