import type { ProjectWorker, WorkerSession } from '../models';
import { post, request } from './http';

/** Registered Workers assigned to the Project with their observed CLI-session state. Read-only: starts nothing. */
export const listProjectWorkers = (projectId: string) => request<ProjectWorker[]>(`/projects/${projectId}/workers`);
/** «Проверить сессию»: re-observes tmux/process/acknowledgement. Never starts a CLI or sends instructions. */
export const getWorkerSession = (projectId: string, agentId: string) =>
  request<WorkerSession>(`/projects/${projectId}/workers/${agentId}/session`);
/** «Активировать сессию»: the one explicit Owner action that may create/reconnect the CLI session. Not a Run. */
/** «Подготовить контекст»: separate explicit Owner action; asks an already active CLI to read the project entry points. */
export const bootstrapWorkerSession = (projectId: string, agentId: string) =>
  post<WorkerSession>(`/projects/${projectId}/workers/${agentId}/session/bootstrap`);
export const activateWorkerSession = (projectId: string, agentId: string) =>
  post<WorkerSession>(`/projects/${projectId}/workers/${agentId}/session/activate`);
