import type { RepairResult, Task, TaskInput } from '../models';
import { post, request } from './http';

export const listTasks = (projectId: string) => request<Task[]>(`/projects/${projectId}/tasks`);
export const createTask = (projectId: string, input: TaskInput) => post<Task>(`/projects/${projectId}/tasks`, input);
export const reconcileTask = (projectId: string, taskId: string) => post<Task>(`/projects/${projectId}/tasks/${taskId}/reconcile-workspace`);
export const repairTask = (projectId: string, taskId: string) => post<RepairResult<Task>>(`/projects/${projectId}/tasks/${taskId}/repair-workspace`, { confirm_repair: true });
