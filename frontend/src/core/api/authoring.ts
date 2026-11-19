import type { OwnerDecision, OwnerDecisionInput, PromptRevision, SpecRevision, SpecRevisionSummary, SpecSaveInput, PromptSaveInput, TaskAuthoring } from '../models';
import { post, request } from './http';

const base = (projectId: string, taskId: string) => `/projects/${projectId}/tasks/${taskId}`;

export const getTaskAuthoring = (projectId: string, taskId: string) => request<TaskAuthoring>(`${base(projectId, taskId)}/authoring`);
export const listSpecRevisions = (projectId: string, taskId: string) => request<SpecRevisionSummary[]>(`${base(projectId, taskId)}/spec/revisions`);
export const getSpecRevision = (projectId: string, taskId: string, revision: number) => request<SpecRevision>(`${base(projectId, taskId)}/spec/revisions/${revision}`);
export const getPromptRevision = (projectId: string, taskId: string, revision: number) => request<PromptRevision>(`${base(projectId, taskId)}/prompt/revisions/${revision}`);
export const saveSpec = (projectId: string, taskId: string, input: SpecSaveInput) =>
  post<{ created: boolean; spec: SpecRevision }>(`${base(projectId, taskId)}/spec/revisions`, input);
export const savePrompt = (projectId: string, taskId: string, input: PromptSaveInput) =>
  post<{ created: boolean; prompt: PromptRevision }>(`${base(projectId, taskId)}/prompt/revisions`, input);
export const submitOwnerDecision = (projectId: string, taskId: string, runId: string, input: OwnerDecisionInput) =>
  post<{ recorded: boolean; replayed: boolean; decision: OwnerDecision; task_status: string }>(`${base(projectId, taskId)}/runs/${runId}/decision`, input);
export const approvePrompt = (projectId: string, taskId: string, revision: number, digest: string) =>
  post<{ created: boolean; prompt: PromptRevision }>(`${base(projectId, taskId)}/prompt/revisions/${revision}/approve`, { digest });
