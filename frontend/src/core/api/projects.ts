import type { Project, ProjectInput, RepairResult } from '../models';
import { post, request } from './http';

export const listProjects = () => request<Project[]>('/projects');
export const createProject = (input: ProjectInput) => post<Project>('/projects', input);
export const reconcileProject = (id: string) => post<Project>(`/projects/${id}/reconcile-workspace`);
/** Sends `confirm_repair: true`; callers must hold a RepairConfirmation from an explicit user action. */
export const repairProject = (id: string) => post<RepairResult<Project>>(`/projects/${id}/repair-workspace`, { confirm_repair: true });
