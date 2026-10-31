import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

type Project = { id: string; name: string; description: string; root_path: string; mode: string; docs_entrypoint: string | null; workspace_state: string; workspace_problem: string | null; architect: string; active_workers: number };
type Task = { id: string; project_id: string; title: string; description: string; acceptance_criteria: string; status: string; workspace_state: string; workspace_problem: string | null; worker: string };
type Repaired = { repair: { backup_id: string } | null };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try { response = await fetch(`/api${path}`, { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers } }); }
  catch { throw new Error('Не удалось связаться с ядром ROSC. Проверьте, что backend запущен, и повторите.'); }
  if (!response.ok) {
    let detail = response.status >= 500 ? `Ядро ROSC недоступно или вернуло ошибку (HTTP ${response.status}). Проверьте, что backend запущен, и повторите.` : `HTTP ${response.status}`;
    try { const json = await response.json(); detail = typeof json.detail === 'string' ? json.detail : detail; } catch { /* keep status */ }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

function describe(e: unknown): string { return e instanceof Error ? e.message : String(e); }

function WorkspaceActions({ state, problem, onReconcile, onRepair }: { state: string; problem: string | null; onReconcile: () => void; onRepair: () => void }) {
  const [confirmed, setConfirmed] = useState(false);
  if (state === 'ready') return null;
  if (problem === 'corrupt' || problem === 'drift') return <div className="repair"><p>{problem === 'corrupt' ? 'Манифест Workspace повреждён.' : 'Манифест Workspace расходится с записью в базе.'} Перед заменой текущий файл будет сохранён в карантин ROSC.</p><label className="check"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} /> Подтверждаю замену манифеста данными из базы</label><button disabled={!confirmed} onClick={onRepair}>Восстановить манифест</button></div>;
  if (problem === 'foreign' || problem === 'unsafe') return <p className="repair">Манифест принадлежит другой записи или лежит в небезопасном месте. Автоматическое исправление отключено, нужно ручное разрешение.</p>;
  return <button onClick={onReconcile}>Проверить Workspace</button>;
}

function route(): { project?: string; task?: string; newTask?: boolean } {
  const pieces = location.pathname.split('/').filter(Boolean);
  return pieces[0] === 'projects' ? { project: pieces[1], task: pieces[2] === 'tasks' ? pieces[3] : undefined, newTask: pieces[2] === 'new-task' } : {};
}

function App() {
  const [locationKey, setLocationKey] = useState(location.pathname);
  const [projects, setProjects] = useState<Project[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [projectsStatus, setProjectsStatus] = useState<'loading' | 'ready' | 'failed'>('loading');
  const [tasksStatus, setTasksStatus] = useState<'loading' | 'ready' | 'failed'>('loading');
  const [newProject, setNewProject] = useState(false);
  const selected = route();
  const project = projects.find(p => p.id === selected.project);
  const task = tasks.find(t => t.id === selected.task);

  function navigate(path: string) { history.pushState(null, '', path); setLocationKey(path); setError(''); setNotice(''); }
  async function loadProjects(quiet = false) { try { setProjects(await request<Project[]>('/projects')); setProjectsStatus('ready'); } catch (e) { setProjectsStatus('failed'); if (!quiet) setError(describe(e)); } }
  async function loadTasks(id: string, quiet = false) { try { setTasks(await request<Task[]>(`/projects/${id}/tasks`)); setTasksStatus('ready'); } catch (e) { setTasksStatus('failed'); if (!quiet) setError(describe(e)); } }

  useEffect(() => { const onPop = () => setLocationKey(location.pathname); window.addEventListener('popstate', onPop); loadProjects(); return () => window.removeEventListener('popstate', onPop); }, []);
  useEffect(() => { setTasksStatus('loading'); }, [selected.project]);
  useEffect(() => { if (selected.project) loadTasks(selected.project); else setTasks([]); }, [selected.project, locationKey]);

  async function submitProject(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault(); setError('');
    const form = new FormData(e.currentTarget);
    const payload = { name: String(form.get('name')), description: String(form.get('description')), root_path: String(form.get('root_path')), mode: String(form.get('mode')), docs_entrypoint: String(form.get('docs_entrypoint')).trim() || null, no_external_agent_confirmed: form.get('no_external_agent_confirmed') === 'on' };
    try { const created = await request<Project>('/projects', { method: 'POST', body: JSON.stringify(payload) }); await loadProjects(); setNewProject(false); navigate(`/projects/${created.id}`); } catch (ex) { setError(describe(ex)); void loadProjects(true); }
  }

  async function submitTask(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault(); setError('');
    if (!project) return;
    const form = new FormData(e.currentTarget);
    const payload = { title: String(form.get('title')), description: String(form.get('description')), acceptance_criteria: String(form.get('acceptance_criteria')) };
    try { const created = await request<Task>(`/projects/${project.id}/tasks`, { method: 'POST', body: JSON.stringify(payload) }); await loadTasks(project.id); navigate(`/projects/${project.id}/tasks/${created.id}`); } catch (ex) { setError(describe(ex)); void loadTasks(project.id, true); }
  }

  async function retryAll() { setError(''); setNotice(''); await loadProjects(); if (selected.project) await loadTasks(selected.project); }

  async function reconcile() {
    if (!project) return;
    try { await request<Project>(`/projects/${project.id}/reconcile-workspace`, { method: 'POST' }); await loadProjects(); setError(''); } catch (ex) { setError(describe(ex)); void loadProjects(true); }
  }

  async function repair() {
    if (!project) return;
    try { const done = await request<Repaired>(`/projects/${project.id}/repair-workspace`, { method: 'POST', body: JSON.stringify({ confirm_repair: true }) }); await loadProjects(); setError(''); setNotice(done.repair ? `Манифест восстановлен. Копия сохранена в карантин: ${done.repair.backup_id}` : 'Манифест уже в порядке.'); } catch (ex) { setError(describe(ex)); void loadProjects(true); }
  }

  async function repairTask() {
    if (!project || !task) return;
    try { const done = await request<Repaired>(`/projects/${project.id}/tasks/${task.id}/repair-workspace`, { method: 'POST', body: JSON.stringify({ confirm_repair: true }) }); await loadTasks(project.id); setError(''); setNotice(done.repair ? `Манифест восстановлен. Копия сохранена в карантин: ${done.repair.backup_id}` : 'Манифест уже в порядке.'); } catch (ex) { setError(describe(ex)); void loadTasks(project.id, true); }
  }

  async function reconcileTask() {
    if (!project || !task) return;
    try { await request<Task>(`/projects/${project.id}/tasks/${task.id}/reconcile-workspace`, { method: 'POST' }); await loadTasks(project.id); setError(''); } catch (ex) { setError(describe(ex)); void loadTasks(project.id, true); }
  }

  const layer = newProject ? 'modal' : (selected.task || selected.newTask) && project ? 'task' : selected.project ? 'project' : 'dashboard';
  const alert = (where: string) => layer === where && <>{error && <div role="alert" className="error">{error}</div>}{notice && <div role="status" className="notice">{notice}</div>}</>;

  return <>
    <header className="topbar"><strong className="logo">ROSC</strong><span className="crumb">/ Рабочее пространство</span><span className="spacer" /><span className="subtle">Engine Slice 01 · агенты не запущены</span></header>
    <main className="dashboard">
      <div className="page-title"><div><p className="eyebrow">ПРОЕКТЫ</p><h1>Рабочее пространство</h1><p>Проекты и задачи доступны без запуска Architect или Worker.</p></div><button className="primary" onClick={() => setNewProject(true)}>＋ Добавить проект</button></div>
      {alert('dashboard')}
      {projects.length === 0 ? <section className="empty">Проектов пока нет. Подключите существующий или зарегистрируйте новый пустой каталог.</section> : <div className="cards">{projects.map(p => <button className="project-card" key={p.id} onClick={() => navigate(`/projects/${p.id}`)}><span className="id">{p.id}</span><h2>{p.name}</h2><p>{p.description}</p><div className="card-foot"><span className="pill">Architect: не активен</span><span className="pill">Workers: 0</span><span className={p.workspace_state === 'ready' ? 'pill ok' : 'pill warning'}>{p.workspace_state === 'ready' ? 'Workspace готов' : 'Нужна сверка Workspace'}</span></div></button>)}</div>}
    </main>
    {selected.project && <div className="backdrop" onClick={() => navigate('/')}><section className="drawer project-drawer" onClick={e => e.stopPropagation()} aria-label="Project Work Area"><button className="edge-close" onClick={() => navigate('/')} aria-label="Закрыть проект">×</button><div className="drawer-head"><div><p className="eyebrow">PROJECT / WORK AREA</p><h2>{project?.name ?? selected.project}</h2><p>{project?.root_path}</p></div><span className="pill">Агенты не запущены</span></div><div className="drawer-body">{alert('project')}{!project && (projectsStatus === 'loading' ? <p>Загрузка…</p> : projectsStatus === 'failed' ? <p>Не удалось загрузить проект. <button onClick={retryAll}>Повторить</button></p> : <p>Проект не найден. Закройте окно и выберите проект на дашборде.</p>)}{project && <><div className="project-summary"><h3>Контекст проекта</h3><p>{project.description}</p><div><span>Architect: не активен</span><span>Workers: 0</span><span>Workspace: {project.workspace_state === 'ready' ? 'готов' : 'нужна сверка'}</span></div><WorkspaceActions state={project.workspace_state} problem={project.workspace_problem} onReconcile={reconcile} onRepair={repair} /></div><div className="tabs"><button className="active">Tasks ({tasks.length})</button><button disabled>Activity</button><button disabled>Настройки проекта</button></div><div className="task-toolbar"><h3>Задачи проекта</h3><button className="primary" onClick={() => navigate(`/projects/${project.id}/new-task`)}>＋ Новая задача</button></div><div className="task-list">{tasksStatus === 'failed' ? <p>Не удалось загрузить задачи. <button onClick={retryAll}>Повторить</button></p> : tasks.length === 0 ? <p>{tasksStatus === 'loading' ? 'Загрузка…' : 'Задач пока нет.'}</p> : tasks.map(t => <button key={t.id} onClick={() => navigate(`/projects/${project.id}/tasks/${t.id}`)}><span>{t.id} · {t.title}</span><span className="pill">{t.status}</span></button>)}</div></>}</div></section></div>}
    {selected.newTask && project && <div className="nested-backdrop" onClick={() => navigate(`/projects/${project.id}`)}><section className="drawer task-drawer" onClick={e => e.stopPropagation()} aria-label="Новая Task"><button className="edge-close second" onClick={() => navigate(`/projects/${project.id}`)} aria-label="Закрыть новую задачу">×</button><div className="drawer-head"><div><p className="eyebrow">TASK / DRAFT</p><h2>Новая задача</h2><p>{project.name} · создание не запускает Worker</p></div></div><form className="drawer-body task-form" onSubmit={submitTask}>{alert('task')}<label>Название<input name="title" required maxLength={160} /></label><label>Описание и контекст<textarea name="description" required rows={6} /></label><label>Критерии приёмки<textarea name="acceptance_criteria" required rows={5} /></label><p className="subtle">Draft сохраняется в Core. Prompt и Run ещё не создаются.</p><button className="primary" type="submit">Создать Task Draft</button></form></section></div>}
    {selected.task && project && <div className="nested-backdrop" onClick={() => navigate(`/projects/${project.id}`)}><section className="drawer task-drawer" onClick={e => e.stopPropagation()} aria-label="Task Work Area"><button className="edge-close second" onClick={() => navigate(`/projects/${project.id}`)} aria-label="Закрыть задачу">×</button><div className="drawer-head"><div><p className="eyebrow">TASK / WORK AREA</p><h2>{task ? `${task.id} · ${task.title}` : selected.task}</h2><p>{project.name} · чтение Task не запускает агента</p></div>{task && <span className="pill">{task.status}</span>}</div><div className="drawer-body">{alert('task')}{!task && (tasksStatus === 'loading' ? <p>Загрузка…</p> : tasksStatus === 'failed' ? <p>Не удалось загрузить задачу. <button onClick={retryAll}>Повторить</button></p> : <p>Задача не найдена в этом проекте.</p>)}{task && <><div className="project-summary"><h3>Спецификация задачи · Draft</h3><h4>Описание</h4><p>{task.description}</p><h4>Критерии приёмки</h4><p>{task.acceptance_criteria}</p></div><div className="project-summary"><h3>Контекст и действия</h3><p>Task Draft сохранена. Architect и Worker не назначены и не запущены.</p><p>Workspace: {task.workspace_state === 'ready' ? 'готов' : 'нужна сверка'}</p><WorkspaceActions state={task.workspace_state} problem={task.workspace_problem} onReconcile={reconcileTask} onRepair={repairTask} /><p>Подготовка Prompt и разрешение Run появятся в следующих срезах.</p></div></>}</div></section></div>}
    {newProject && <div className="modal-back" onClick={() => setNewProject(false)}><form className="modal" onSubmit={submitProject} onClick={e => e.stopPropagation()}><div className="modal-title"><h2>Добавить проект</h2><button type="button" onClick={() => setNewProject(false)} aria-label="Закрыть">×</button></div>{alert('modal')}<label>Режим<select name="mode" defaultValue="existing"><option value="existing">Подключить существующий</option><option value="new">Зарегистрировать новый пустой каталог</option></select></label><label>Название<input name="name" required maxLength={160} /></label><label>Описание<textarea name="description" required rows={2} /></label><label>Корневая директория<input name="root_path" required placeholder="/home/user/projects/example" /></label><label>Файл документации, необязательно<input name="docs_entrypoint" placeholder="README.md" /></label><label className="check"><input type="checkbox" name="no_external_agent_confirmed" /> В этом checkout сейчас не работает внешний агент (для существующего проекта)</label><p className="subtle">Каталог уже должен существовать. ROSC создаст свои файлы отдельно от исходников проекта.</p><button className="primary" type="submit">Сохранить проект</button></form></div>}
  </>;
}

createRoot(document.getElementById('root')!).render(<App />);
