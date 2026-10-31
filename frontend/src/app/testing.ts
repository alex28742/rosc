// Test fixtures shared by the application tests. Not imported by production code.
import { vi } from 'vitest';
import type { CoreApi } from '../core/api';
import type { Project, Task } from '../core/models';

export const project = (id: string, over: Partial<Project> = {}): Project => ({
  id, name: `Project ${id}`, description: `About ${id}`, root_path: `/tmp/${id}`, mode: 'existing', docs_entrypoint: null,
  workspace_state: 'ready', workspace_problem: null, architect: 'not_active', active_workers: 0, created_at: '2026-10-31T00:00:00Z', ...over,
});

export const task = (id: string, projectId: string, over: Partial<Task> = {}): Task => ({
  id, project_id: projectId, title: `Task ${id}`, description: `Do ${id}`, acceptance_criteria: `Done ${id}`, status: 'Draft',
  workspace_state: 'ready', workspace_problem: null, worker: 'not_assigned', created_at: '2026-10-31T00:00:00Z', ...over,
});

export function stubApi(over: Partial<{ [K in keyof CoreApi]: ReturnType<typeof vi.fn> }> = {}) {
  const api = {
    listProjects: vi.fn(async () => [] as Project[]),
    createProject: vi.fn(async () => project('P0001')),
    reconcileProject: vi.fn(async () => project('P0001')),
    repairProject: vi.fn(async () => ({ ...project('P0001'), repair: null })),
    listTasks: vi.fn(async () => [] as Task[]),
    createTask: vi.fn(async () => task('T0001', 'P0001')),
    reconcileTask: vi.fn(async () => task('T0001', 'P0001')),
    repairTask: vi.fn(async () => ({ ...task('T0001', 'P0001'), repair: null })),
    ...over,
  };
  return api as typeof api & CoreApi;
}

export function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}
